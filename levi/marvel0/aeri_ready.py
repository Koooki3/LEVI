"""The passive readiness of an AERI run, and the document of the preparation card
(``GET /api/levi/marvel0/aeri``, ``levi.marvel0.aeri.v1``).

**Passive**: every row is read from a file, ``/proc``, ``nvidia-smi`` (query
options, no CUDA context), ``systemctl --user show/list-units``, ``git`` (no
optional lock) or a read-only view of another part of the page. No port is
connected to, no lock is taken, no unit started, nothing written. The arm's
pose cannot be read passively (it needs the robot server's port), so "the arm is
at home" stays a tick of the person's and the check of AERI's own start gate.

**Every row has a level**: ``ok``, ``warn`` (shown, never refuses),
``refuse`` or ``unknown``. **``unknown`` refuses as ``refuse`` does**: a source
that cannot be read is never taken for a good one. The card is ``ready`` only
when it has rows and none refuses or is unknown.

The decision of each row is a function of plain facts (``rows``); the facts are
read by ``collect`` through ``Host`` (a test replaces the seam, never the
decision). ``Readiness.snapshot`` is what the hosted run asks at its challenge
and at its start (always a fresh read); the ``sha256`` it returns covers each
row's code and level, the profile, the AERI HEAD and the prepared run's
digests, but not the numbers in ``params``, so a number that drifts does not
void a challenge and any row changing level does.

**Cost.** A full read is one batch (git three times, ``nvidia-smi``, two
``systemctl``, the stack and deploy views) and the page asks every three seconds,
from every open tab, also while the arm controller runs at 1 kHz on a kernel
without real-time scheduling. So a read is **light** (no process started, no
folder scanned: the control file, the site file, the profile table and two
in-memory answers) whenever the rest cannot matter: ``disabled`` (the control
file switches AERI off: the document is the switch and the site's facts) and
``busy`` (this page's own terminal D, or its evaluation client, is on: a start is
refused whatever the other rows say, and the arm controller is up, so the one
row that says why is all there is). A full read is cached for ``CACHE_S``,
well under the page's poll, so two tabs share one batch and every poll still
sees what is now.

No answer holds a path, except the ``execute`` of the prepared run, which is the
command for the terminal fallback and has to show them.
"""

import hashlib
import json
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from levi import paths
from levi.live import gpumgr
from levi.setup import probes
from levi.workspace import Workspace

from . import aeri_leftovers, aeri_prep, control_site, net, record, status
from . import site as marvel0_site

SCHEMA = "levi.marvel0.aeri.v1"
# Well below the page's poll (3 s): a read is shared by callers that arrive together
# (two tabs, a challenge and a poll), never served to the next poll.
CACHE_S = 1.0
LIVE_PORT = 8100
PORTS = (8000, 8100, 8110)  # the policy server, the VLM, the bridge experiment
MEM_NEED_MIB = 28 * 1024  # AERI.md section 2: available >= 28 GB
SHOW_TIMEOUT_S = 3.0
GPU_TIMEOUT_S = 5.0

ROW_CODES = (
    "aeri_enabled",
    "product",
    "aeri_site",
    "code_pin",
    "profile_known",
    "prompts_checked",
    "templates_sha",
    "prepared",
    "run_name_free",
    "cameras_config",
    "gpu_free",
    "gpu_lock_free",
    "mem_available",
    "ports_free",
    "live_service",
    "arm_lock_free",
    "no_e_run",
    "no_policy_deploy",
    "no_aeri_unit_left",
    "swap_logs_clean",
    "no_other_aeri",
    "monitor_green",
    "stack_ready",
)
# These never refuse: shown as ``warn`` when they cannot be fully confirmed.
WARN_ROWS = ("stack_ready",)
# The contract with the start gate (``aeri_run._start_gate``): a start needs
# ``snapshot()["ready"] is True`` and no row below ``ok`` except these, which may
# be ``warn`` (``stack_ready`` can only ever be ``warn`` when the stack is up: the
# gripper stack and the controller cannot be observed from here). Any other row
# at ``warn`` is a bug of the row and refuses the start.
WARN_OK = WARN_ROWS

CHECK_OK = {
    "aeri_enabled": "AERI hosting is switched on in the control file.",
    "product": "This is the product LEVI.",
    "aeri_site": "The site file has a valid AERI section.",
    "code_pin": "The AERI code is at the pinned commit and clean.",
    "profile_known": "The pinned code has a known prompt profile.",
    "prompts_checked": "The AERI code's source files are the ones the prompt "
    "profile was written from.",
    "templates_sha": "The job and binding templates match SOURCES.sha256.",
    "prepared": "Run {name} is prepared and unchanged.",
    "run_name_free": "The name is not used by a rollout folder or an AERI registry.",
    "cameras_config": "The binding's camera serials equal the site file's.",
    "gpu_free": "Enough GPU memory is free.",
    "gpu_lock_free": "The GPU lock is free.",
    "mem_available": "Enough memory is available.",
    "ports_free": "Ports 8000, 8100 and 8110 are free.",
    "live_service": "The live annotation service is not running.",
    "arm_lock_free": "The arm lock is free.",
    "no_e_run": "No hosted evaluation client or session is on.",
    "no_policy_deploy": "This page has no policy server unit.",
    "no_aeri_unit_left": "No AERI model or policy unit is left behind.",
    "swap_logs_clean": "No unfinished policy-switch log.",
    "no_other_aeri": "No other AERI run is alive.",
    "monitor_green": "The health monitor is fresh and green.",
    "stack_ready": "The arm stack looks up (the gripper stack and the controller "
    "are not observable from here).",
}
CHECK_REFUSE = {
    "aeri_enabled": "AERI hosting is off in the control file.",
    "product": "Only the product LEVI may act; this instance only shows.",
    "aeri_site": "The site file has no valid AERI section.",
    "code_pin": "The AERI code is not at the pinned commit, or its tree is dirty.",
    "profile_known": "The pinned code has no known prompt profile.",
    "prompts_checked": "The AERI code differs from what the prompt profile was "
    "written from ({reason}: {file}); the page would misread its terminal.",
    "templates_sha": "A template does not match SOURCES.sha256.",
    "prepared": "No run is prepared, or it changed since ({reason}).",
    "run_name_free": "The run name is already used ({where}).",
    "cameras_config": "The binding's camera serials differ from the site file's "
    "({fields}).",
    "gpu_free": "Only {free_mib} MiB of GPU memory are free; {mode} mode needs "
    "{need_mib}.",
    "gpu_lock_free": "The GPU lock is held ({holders} holder(s)).",
    "mem_available": "Only {available_mib} MiB of memory are available; {need_mib} "
    "are needed.",
    "ports_free": "A listener holds a port AERI needs: {ports}.",
    "live_service": "The live annotation service is running and holds port 8100.",
    "arm_lock_free": "The arm lock is held: something else uses the arm.",
    "no_e_run": "A hosted evaluation client or session is on.",
    "no_policy_deploy": "This page's policy server unit exists ({state}); AERI "
    "starts its own.",
    "no_aeri_unit_left": "AERI units are left behind and may hold GPU memory.",
    "swap_logs_clean": "A policy-switch log is unfinished; the run would need the "
    "variant phrase.",
    "no_other_aeri": "Another AERI run is alive.",
    "monitor_green": "The health monitor is not fresh and green ({state}).",
    "stack_ready": "A part of the arm stack is not up.",
}
CHECK_UNKNOWN = {
    "prompts_checked": "The AERI code could not be compared with the prompt "
    "profile ({reason}).",
    "code_pin": "The AERI worktree's git state could not be read.",
    "profile_known": "The profile table could not be read.",
    "templates_sha": "SOURCES.sha256 or a template could not be read.",
    "cameras_config": "The camera serials could not be compared.",
    "gpu_free": "GPU memory could not be read.",
    "gpu_lock_free": "Who holds the GPU lock could not be read.",
    "mem_available": "Memory could not be read.",
    "ports_free": "The socket tables could not be read, so a listener is not ruled "
    "out.",
    "live_service": "The live annotation service's state could not be read.",
    "arm_lock_free": "Who holds the arm lock could not be read.",
    "no_e_run": "The evaluation client's state could not be read.",
    "no_policy_deploy": "The policy server deploy's state could not be read.",
    "no_aeri_unit_left": "The unit list could not be read, so a leftover is not "
    "ruled out.",
    "swap_logs_clean": "The policy-switch logs could not be read.",
    "no_other_aeri": "The AERI run registry could not be read.",
    "monitor_green": "The health monitor could not be read.",
    "stack_ready": "The arm stack's state could not be read.",
    "product": "Which instance this is could not be read.",
}
NOTICES = {
    "gates_scope": "These checks stop mistakes, not another program of the same "
    "account. The real barriers are Franka Desk and the physical emergency stop; "
    "keep a hand on it.",
}
CONFIRM = {
    "prepare": aeri_prep.PHRASE_PREPARE,
    "live_stop": aeri_prep.PHRASE_LIVE_STOP,
    "leftovers": aeri_prep.PHRASE_LEFTOVERS,
}
WHY_TEXT = {
    "not_running": "The live annotation service is not running.",
    "nothing_to_stop": "No leftover AERI unit exists.",
    "aeri_disabled": "AERI hosting is off in the control file.",
    "not_product": "Only the product LEVI may act; this instance only shows.",
}


# --- the machine seam -----------------------------------------------------------------------


def _workspace_state():
    return Workspace.at(paths.workspace()).state


def read_gpu(run=aeri_prep.run_capture):
    """``(used_mib, total_mib)`` summed over the GPUs, or None."""
    try:
        done = run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            timeout=GPU_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return parse_gpu(done.stdout) if done.returncode == 0 else None


def parse_gpu(text):
    """``(used, total)`` MiB of ``nvidia-smi --query-gpu=memory.used,memory.total
    --format=csv,noheader,nounits`` (one ``22, 32607`` line per GPU, summed), or
    None for anything else (no line, a line that is not two whole numbers,
    used above total)."""
    used = total = 0
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    for line in lines:
        m = re.fullmatch(r"\s*(\d+)\s*,\s*(\d+)\s*", line)
        if m is None:
            return None
        used += int(m.group(1))
        total += int(m.group(2))
    return (used, total) if used <= total else None


def read_meminfo():
    try:
        data, truncated, _ = probes.read_regular("/proc/meminfo", 1 << 16)
    except (OSError, probes.NotRegular):
        return None
    return None if truncated else data.decode("utf-8", "replace")


def parse_meminfo(text):
    """``MemAvailable`` in MiB, or None."""
    m = re.search(r"(?m)^MemAvailable:\s+(\d+)\s+kB\s*$", text or "")
    return int(m.group(1)) // 1024 if m else None


def show_unit(unit, run=aeri_prep.run_capture):
    """``{"load", "active", "sub"}`` of a user unit (``systemctl --user show``;
    a unit that does not exist is ``load: not-found``), or None."""
    try:
        done = run(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "-p",
                "LoadState",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
            ],
            timeout=SHOW_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if done.returncode != 0:
        return None
    return parse_show(done.stdout)


def parse_show(text):
    """The unit's ``{"load", "active", "sub"}`` from ``systemctl show`` lines,
    or None when a property is missing."""
    table = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            table[key] = value.strip()
    if not all(k in table for k in ("LoadState", "ActiveState", "SubState")):
        return None
    return {
        "load": table["LoadState"],
        "active": table["ActiveState"],
        "sub": table["SubState"],
    }


def live_state(shown):
    """``running``, ``stopped``, ``absent`` or None for the live unit as
    ``show_unit`` read it."""
    if shown is None:
        return None
    if shown["load"] == "not-found":
        return "absent"
    if shown["active"] in aeri_leftovers.ACTIVE_STATES:
        return "running"
    if shown["active"] in ("inactive", "failed"):
        return "stopped"
    return None


def _units():
    return status.units_part()


def _product():
    from . import stack

    try:
        return (
            stack.profile(stack._read_self_cgroup(), stack._in_main_checkout())[
                "profile"
            ]
            == "real"
        )
    except Exception:  # noqa: BLE001
        return None


def _aeri_runs():
    from . import deploy

    return deploy._aeri_runs()


def _swap_pending(root):
    from levi.automatic import fr3_policy_swap

    return fr3_policy_swap.pending(root)


def _stack_view():
    from . import stack_api

    return stack_api.stack_view()


def _deploy_view():
    from . import deploy_api

    return deploy_api.supervisor().status()


def _hosted_e_active():
    from . import run_api

    return run_api.run_active()


def _d_hosted_active():
    """Whether the page's own terminal D is on. A module that does not exist yet
    means there is none; one that exists but cannot be imported or asked is
    unknown (the caller treats that as an obstacle)."""
    import importlib.util

    if importlib.util.find_spec(f"{__package__}.aeri_run_api") is None:
        return False
    from . import aeri_run_api

    return aeri_run_api.active() is not None


def _sessions(root, now):
    return status.sessions_part([root], now)


def _profile_matches(worktree, profile_id):
    """``(ok, detail)`` of the hosted run's own comparison of the worktree's
    source files with the profile's recorded digests, or None when that module
    cannot be read."""
    try:
        from . import aeri_state
    except ImportError:
        return None
    return aeri_state.profile_matches(worktree, profile_id)


@dataclass
class Host:
    """Everything the readiness reads outside Python. A test replaces single
    fields; ``production_host()`` is the real one."""

    now: Callable = time.time
    monotonic: Callable = time.monotonic
    prep: aeri_prep.Host = field(default_factory=aeri_prep.production_host)
    control: Callable = control_site.read
    site: Callable = marvel0_site.load
    product: Callable = _product
    gpu: Callable = read_gpu
    meminfo: Callable = read_meminfo
    net_table: Callable = net.read
    flock: Callable = lambda path: gpumgr.flock_holders(path)
    unit_show: Callable = show_unit
    units: Callable = _units
    aeri_runs: Callable = _aeri_runs
    swap_pending: Callable = _swap_pending
    stack_view: Callable = _stack_view
    deploy_view: Callable = _deploy_view
    hosted_e: Callable = _hosted_e_active
    hosted_d: Callable = _d_hosted_active
    sessions: Callable = _sessions
    profile_matches: Callable = _profile_matches
    state_root: Callable = _workspace_state


def production_host() -> Host:
    return Host()


# --- the facts ------------------------------------------------------------------------------


# What a light read leaves unread (``None``: not told, which refuses like an
# unreadable source would, so no row may be built from it: ``rows`` returns the
# light rows instead).
UNREAD = {
    "mode": "external",
    "git": {"head": None, "dirty": None},
    "matches": None,
    "templates": None,
    "binding": None,
    "prepared": None,
    "name_where": None,
    "gpu": None,
    "mem_mib": None,
    "table": None,
    "gpu_lock": None,
    "arm_lock": None,
    "live": None,
    "units": None,
    "aeri_runs": None,
    "swap": None,
    "stack": None,
    "deploy": None,
    "e_hosted": None,
    "e_session": None,
    "d_hosted": None,
}


def _try(call, *args):
    """``call(*args)``, or None when it raises: a source that fails is an
    unknown fact, never a failed request."""
    try:
        return call(*args)
    except Exception:  # noqa: BLE001
        return None


def collect(host: Host) -> dict:
    """The facts, each from its own source; ``None`` for what could not be read.
    Reads nothing that needs the AERI section when it is absent."""
    ctl_load = _try(host.control)
    control = (
        control_site.aeri_view(ctl_load)
        if ctl_load is not None
        else {
            "state": "invalid",
            "code": "aeri.invalid",
            "enabled": False,
            "errors": [],
        }
    )
    site_load = _try(host.site)
    aeri = None
    main_site = None
    if site_load is None:
        site_view = {"state": "invalid", "code": "aeri_site.invalid", "errors": []}
    elif site_load.state == "missing":
        site_view = {
            "state": "unconfigured",
            "code": "aeri_site.missing_file",
            "errors": [],
        }
    elif site_load.state == "invalid":
        site_view = {
            "state": "invalid",
            "code": "aeri_site.invalid",
            "errors": [dict(e) for e in site_load.errors],
        }
    else:
        main_site = site_load.site
        aeri = main_site.aeri
        if aeri is not None:
            site_view = {"state": "ok", "code": "aeri_site.ok", "errors": []}
        elif main_site.aeri_errors:
            site_view = {
                "state": "invalid",
                "code": "aeri_site.invalid",
                "errors": [{"field": f, "code": c} for f, c in main_site.aeri_errors],
            }
        else:
            site_view = {
                "state": "unconfigured",
                "code": "aeri_site.no_section",
                "errors": [],
            }
    facts = {
        "control": control,
        "site": site_view,
        "aeri": aeri,
        "main_site": main_site,
        "sampled_at": host.now(),
        "product": _try(host.product),
    }
    if aeri is None:
        return facts
    # The light reads: nothing below this block is read (no process, no folder).
    facts.update(light=None, **{**UNREAD, "git": dict(UNREAD["git"])})
    facts["profile"] = aeri_prep.profile_info(aeri.profile)
    if control["enabled"] is not True:
        facts["light"] = "disabled"
        return facts
    hosted_d, hosted_e = _try(host.hosted_d), _try(host.hosted_e)
    if hosted_d is True or hosted_e is True:
        facts.update(light="busy", d_hosted=hosted_d, e_hosted=hosted_e)
        return facts
    state = _try(aeri_prep.git_state, aeri.worktree, host.prep.run) or {
        "head": None,
        "dirty": None,
    }
    page_dir = _try(_page_dir, host, aeri)
    page_record = None if page_dir is None else aeri_prep.read_page_record(page_dir)
    prepared = (
        None
        if page_record is None
        else _try(aeri_prep.read_prepared, aeri, page_record["name"], state["head"])
    )
    # The GPU threshold follows the prepared run's mode; external until one is.
    mode = (
        prepared["mode"]
        if prepared and prepared.get("mode") in aeri_prep.MODES
        else "external"
    )
    templates = _try(aeri_prep.templates_state, aeri.templates_root)
    registry = _try(host.prep.registry)
    name = prepared["name"] if prepared else None
    where = (
        None
        if name is None or registry is None
        else [
            w
            for w in aeri_prep.name_where(aeri, registry, name)
            if w not in ("runs_root", "page")
        ]
    )
    table = _try(host.net_table)
    units_part = _try(host.units)
    units = aeri_leftovers.units_items(units_part) if units_part is not None else None
    facts.update(
        {
            "mode": mode,
            "git": state,
            "profile": aeri_prep.profile_info(aeri.profile),
            "matches": _try(host.profile_matches, aeri.worktree, aeri.profile),
            "templates": None
            if templates is None
            else {k: templates[k] for k in ("ok", "which", "digests")},
            "binding": _try(_binding_serials, aeri, prepared, templates),
            "prepared": prepared,
            "name_where": where,
            "gpu": _try(host.gpu),
            "mem_mib": _try(lambda: parse_meminfo(host.meminfo())),
            "table": table if table is not None and table.ok else None,
            "gpu_lock": _try(host.flock, main_site.gpu_lock_file),
            "arm_lock": _try(host.flock, main_site.arm_lock_file),
            "live": _try(lambda: live_state(host.unit_show(main_site.live.unit))),
            "units": units,
            "aeri_runs": _try(host.aeri_runs),
            "swap": _try(host.swap_pending, aeri.rollout_root),
            "stack": _try(host.stack_view),
            "deploy": _try(host.deploy_view),
            "e_hosted": hosted_e,
            "e_session": _try(_e_session, host, main_site),
            "d_hosted": hosted_d,
        }
    )
    return facts


def _page_dir(host, aeri):
    return aeri_prep.state_dir(
        aeri, aeri_prep.StateRoot(host.state_root()), create=False
    )


def _binding_serials(aeri, prepared, templates):
    """``{"side", "wrist"}`` camera serials of the binding the next run uses: the
    prepared run's file, else the template's. None when it cannot be read."""
    blob = None
    if prepared and prepared.get("name"):
        blob = aeri_prep._read_file(
            Path(aeri.runs_root) / prepared["name"] / "binding.json"
        )
    if blob is None and templates:
        blob = (templates.get("data") or {}).get("binding.base.json")
    if blob is None:
        return None
    try:
        cams = json.loads(blob)["cameras"]
        return {"side": cams["side"], "wrist": cams["wrist"]}
    except (ValueError, KeyError, TypeError):
        return None


def _e_session(host, main_site):
    """True when an evaluation session has not ended (the client of a terminal
    counts), False when none, None when it cannot be told."""
    part = host.sessions(main_site.rollout_root, host.now())
    if not isinstance(part, dict) or part.get("state") != "ok":
        return None
    return any(
        row.get("status") not in status.ENDED_STATES
        and (row.get("age_s") or 0) <= status.SESSION_BUSY_S
        for row in part.get("sessions") or []
    )


# --- the rows -------------------------------------------------------------------------------


def _fill(text, params):
    try:
        return text.format(**params)
    except (KeyError, IndexError, ValueError):
        return text


def row(code, ok, **params) -> dict:
    """One row. ``ok`` True passes, False refuses, None means it could not be
    told (that refuses too). A row of ``WARN_ROWS`` that passes shows ``warn``
    when ``warn=True`` is among the params (taken out of them)."""
    warn = params.pop("warn", False)
    if ok is True:
        level, text = ("warn" if warn else "ok"), CHECK_OK[code]
    elif ok is False:
        level, text = "refuse", CHECK_REFUSE[code]
    else:
        level, text = "unknown", CHECK_UNKNOWN.get(code, CHECK_REFUSE[code])
    return {"code": code, "level": level, "text": _fill(text, params), "params": params}


def _listening(table, port):
    return bool(net.listeners(table, port))


def row_prompts(facts):
    """The AERI worktree's source files against the digests the profile was
    written from (``aeri_state.profile_matches``): the page's reading of
    the terminal is right only for the code it was written from."""
    got = facts["matches"]
    if not (isinstance(got, tuple) and len(got) == 2):
        return row("prompts_checked", None, reason="unavailable", file=None)
    ok, detail = got
    if ok is True:
        return row("prompts_checked", True)
    reason, _, file = str(detail).partition(":")
    if reason == "unreadable":  # a file that cannot be read cannot be compared
        return row("prompts_checked", None, reason=reason, file=file or None)
    return row("prompts_checked", False, reason=reason, file=file or None)


def row_stack(stack_doc):
    """A and the robot server (C) decide; M is the monitor row's; B has no
    passive evidence at all (``stack.terminals``) and is only listed."""
    cards = {"a": "unknown", "b": "unknown", "c": "unknown", "m": "unknown"}
    if not isinstance(stack_doc, dict):
        return row("stack_ready", None, cards=cards)
    terms = stack_doc.get("terminals")
    if not isinstance(terms, dict):
        return row("stack_ready", None, cards=cards)
    for key in cards:
        value = terms.get(key.upper())
        if isinstance(value, dict) and isinstance(value.get("state"), str):
            cards[key] = value["state"]
    a, c = cards["a"], cards["c"]
    if a == "unknown" or c == "unknown":
        return row("stack_ready", None, cards=cards)
    if a != "running" or c != "running":
        return row("stack_ready", False, cards=cards)
    return row("stack_ready", True, warn=True, cards=cards)


def rows(facts) -> list:
    """The readiness rows for these facts, in ``ROW_CODES`` order; no rows when
    there is no AERI section (nothing to be ready for)."""
    aeri = facts.get("aeri")
    if aeri is None or facts.get("light") == "disabled":
        return []
    if facts.get("light") == "busy":
        # Whichever of the two is on says why nothing else is asked.
        if facts["d_hosted"] is True:
            return [row("no_other_aeri", False)]
        return [row("no_e_run", False)]
    out = []
    ctl = facts["control"]
    out.append(row("aeri_enabled", ctl["enabled"] is True, state=ctl["state"]))
    out.append(row("product", facts["product"]))
    out.append(row("aeri_site", True))
    git = facts["git"]
    if git["head"] is None or git["dirty"] is None:
        out.append(
            row("code_pin", None, head=None, pin=aeri.pin_sha, dirty=git["dirty"])
        )
    else:
        ok = aeri_prep.pin_matches(aeri.pin_sha, git["head"]) and not git["dirty"]
        out.append(
            row(
                "code_pin",
                ok,
                head=git["head"][:8],
                pin=aeri.pin_sha[:8],
                dirty=git["dirty"],
            )
        )
    out.append(row("profile_known", facts["profile"]["known"], profile=aeri.profile))
    out.append(row_prompts(facts))
    tpl = facts["templates"]
    out.append(
        row(
            "templates_sha",
            None if tpl is None else tpl["ok"],
            which=[] if tpl is None else tpl["which"],
        )
    )
    prepared = facts["prepared"]
    if prepared is None:
        out.append(row("prepared", False, name=None, reason="none"))
    else:
        problem = prepared["problems"][0] if prepared["problems"] else None
        out.append(
            row("prepared", prepared["intact"], name=prepared["name"], reason=problem)
        )
    where = facts["name_where"]
    name = prepared["name"] if prepared else None
    out.append(
        row(
            "run_name_free",
            None if prepared and where is None else not where,
            name=name,
            where=[] if where is None else where,
        )
    )
    serials = facts["binding"]
    if serials is None or facts["main_site"] is None:
        out.append(row("cameras_config", None, fields=[]))
    else:
        cams = facts["main_site"].cameras
        different = [
            n
            for n, want in (("side", cams.view), ("wrist", cams.wrist))
            if serials.get(n) != want
        ]
        out.append(row("cameras_config", not different, fields=different))
    mode = facts["mode"]
    need = aeri_prep.GPU_NEED_MIB[mode]
    gpu = facts["gpu"]
    if gpu is None:
        out.append(row("gpu_free", None, free_mib=None, need_mib=need, mode=mode))
    else:
        free = gpu[1] - gpu[0]
        out.append(
            row("gpu_free", free >= need, free_mib=free, need_mib=need, mode=mode)
        )
    holders = facts["gpu_lock"]
    out.append(
        row(
            "gpu_lock_free",
            None if holders is None else not holders,
            holders=None if holders is None else len(holders),
        )
    )
    mem = facts["mem_mib"]
    out.append(
        row(
            "mem_available",
            None if mem is None else mem >= MEM_NEED_MIB,
            available_mib=mem,
            need_mib=MEM_NEED_MIB,
        )
    )
    table = facts["table"]
    busy = None if table is None else [p for p in PORTS if _listening(table, p)]
    out.append(row("ports_free", None if busy is None else not busy, ports=busy or []))
    live = facts["live"]
    out.append(
        row(
            "live_service",
            None if live is None else live != "running",
            state=live or "unknown",
        )
    )
    arm = facts["arm_lock"]
    out.append(row("arm_lock_free", None if arm is None else not arm))
    e_hosted, e_session = facts["e_hosted"], facts["e_session"]
    if e_hosted is True or e_session is True:
        e_ok = False
    elif e_hosted is None or e_session is None:
        e_ok = None
    else:
        e_ok = True
    out.append(row("no_e_run", e_ok))
    deploy = facts["deploy"]
    dstate = deploy.get("state") if isinstance(deploy, dict) else None
    if dstate is None or dstate == "unknown":
        out.append(row("no_policy_deploy", None, state="unknown"))
    else:
        out.append(
            row("no_policy_deploy", dstate in ("idle", "failed", "dead"), state=dstate)
        )
    items = facts["units"]
    used = None if facts["gpu"] is None else facts["gpu"][0]
    if items is None:
        out.append(row("no_aeri_unit_left", None, units=[], vram_mib=used))
    else:
        left = aeri_leftovers.any_aeri_unit(items)
        out.append(row("no_aeri_unit_left", not left, units=left, vram_mib=used))
    swap = facts["swap"]
    out.append(
        row(
            "swap_logs_clean",
            None if swap is None else not swap,
            count=None if swap is None else len(swap),
        )
    )
    runs = facts["aeri_runs"]
    d_hosted = facts["d_hosted"]
    if d_hosted is True or (
        runs is not None and any(r.get("runner_alive") for r in runs)
    ):
        other_ok = False
    elif runs is None or d_hosted is None:
        other_ok = None
    else:
        other_ok = True
    out.append(row("no_other_aeri", other_ok))
    stack_doc = facts["stack"]
    health = stack_doc.get("health") if isinstance(stack_doc, dict) else None
    hstate = health.get("state") if isinstance(health, dict) else None
    out.append(
        row(
            "monitor_green",
            None if hstate is None or hstate == "unknown" else hstate == "ok",
            state=hstate or "unknown",
        )
    )
    out.append(row_stack(stack_doc))
    return out


def is_ready(rows_) -> bool:
    return bool(rows_) and all(r["level"] in ("ok", "warn") for r in rows_)


def digest(rows_, facts) -> str:
    """The sha256 a challenge and a start must agree on: each row's code and
    level, the profile, the AERI HEAD and the prepared run's digests."""
    aeri = facts.get("aeri")
    prepared = facts.get("prepared")
    body = {
        "rows": [[r["code"], r["level"]] for r in rows_],
        "profile": aeri.profile if aeri else None,
        "head": (facts.get("git") or {}).get("head"),
        "prepared": None
        if not prepared
        else {
            "name": prepared["name"],
            "plan_sha256": prepared["plan_sha256"],
            "binding_sha256": prepared["binding_sha256"],
            "files_sha256": prepared["files_sha256"],
        },
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


# --- the blocks of the document -------------------------------------------------------------


def _why(code):
    return {"code": code, "text": WHY_TEXT.get(code, "")}


def _obstacles(facts):
    d_alive = _any_true(
        facts["d_hosted"],
        None
        if facts["aeri_runs"] is None
        else any(r.get("runner_alive") for r in facts["aeri_runs"]),
    )
    e_active = _any_true(facts["e_hosted"], facts["e_session"])
    arm = facts["arm_lock"]
    gpu = facts["gpu_lock"]
    return {
        "d_alive": d_alive,
        "e_active": e_active,
        "arm_held": None if arm is None else bool(arm),
        "gpu_held": None if gpu is None else bool(gpu),
    }


def _any_true(*values):
    if any(v is True for v in values):
        return True
    if any(v is None for v in values):
        return None
    return False


def live_block(facts, enabled) -> dict:
    live = facts["live"]
    table = facts["table"]
    listener = None if table is None else _listening(table, LIVE_PORT)
    reason = None
    if not enabled:
        reason = _why("aeri_disabled")
    elif facts["product"] is not True:
        reason = (
            _why("not_product")
            if facts["product"] is False
            else {
                "code": "unreadable",
                "text": "A source could not be read, so nothing is offered.",
            }
        )
    elif live is None:
        reason = {
            "code": "unreadable",
            "text": "A source could not be read, so nothing is offered.",
        }
    elif live != "running":
        reason = _why("not_running")
    else:
        reason = aeri_leftovers.why_not(
            **{k: v for k, v in _obstacles(facts).items() if k != "gpu_held"}
        )
    return {
        "state": live or "unknown",
        "port": LIVE_PORT,
        "listener": listener,
        "can_stop": reason is None,
        "why_not": reason,
    }


def leftovers_block(facts, enabled) -> dict:
    items = facts["units"]
    swap = facts["swap"]
    swap_view = {
        "state": "unknown" if swap is None else ("unclean" if swap else "clean"),
        "count": 0 if swap is None else len(swap),
    }
    if items is None:
        return {
            "state": "unknown",
            "units": [],
            "swap": swap_view,
            "can_stop": False,
            "why_not": {
                "code": "unreadable",
                "text": "A source could not be read, so nothing is offered.",
            },
        }
    units = aeri_leftovers.leftover_units(items)
    if not enabled:
        reason = _why("aeri_disabled")
    elif facts["product"] is not True:
        reason = (
            _why("not_product")
            if facts["product"] is False
            else {
                "code": "unreadable",
                "text": "A source could not be read, so nothing is offered.",
            }
        )
    elif not units:
        reason = _why("nothing_to_stop")
    else:
        reason = aeri_leftovers.why_not(**_obstacles(facts))
    return {
        "state": "present" if units else "none",
        "units": units,
        "swap": swap_view,
        "can_stop": reason is None,
        "why_not": reason,
    }


def profile_block(facts):
    aeri = facts["aeri"]
    if aeri is None:
        return None
    git = facts["git"]
    info = facts["profile"]
    dirty = git["dirty"]
    return {
        "id": aeri.profile,
        "known": info["known"] is True,
        "start_home": info["start_home"],
        "console_lang": aeri.console_lang,
        "pin_sha": aeri.pin_sha,
        "head": None if git["head"] is None else git["head"][:8],
        "clean": None if dirty is None else not dirty,
    }


def profiles_block():
    table = aeri_prep.known_profiles() or {}
    return [
        {
            "id": key,
            "start_home": value["start_home"],
            "default": key == marvel0_site.AERI_DEFAULT_PROFILE,
        }
        for key, value in sorted(table.items())
    ]


def limits_block(facts):
    if facts.get("aeri") is None:
        return None
    modes = ["external"] + (["local"] if facts["aeri"].local_model else [])
    return {
        "modes": modes,
        "rounds": dict(aeri_prep.ROUNDS),
        "steps": dict(aeri_prep.STEPS),
        "gripper_confirm_s": dict(aeri_prep.GRIPPER_CONFIRM_S),
        "task": {
            "pattern": aeri_prep.TASK.pattern,
            "max_chars": aeri_prep.TASK_MAX_CHARS,
        },
    }


def document(facts, rows_=None) -> dict:
    """The ``levi.marvel0.aeri.v1`` document of these facts."""
    rows_ = rows(facts) if rows_ is None else rows_
    control = facts["control"]
    site_view = dict(facts["site"])
    site_view["template"] = (
        marvel0_site.AERI_TEMPLATE if site_view["state"] == "unconfigured" else None
    )
    enabled = control["enabled"] is True
    configured = facts.get("aeri") is not None
    full = configured and not facts.get("light")  # (a light read left them unread)
    return {
        "schema": SCHEMA,
        "sampled_at": record.iso(facts["sampled_at"]),
        "enabled": enabled,
        "control": {k: control[k] for k in ("state", "code", "errors")},
        "site": site_view,
        "profile": profile_block(facts),
        "profiles": profiles_block() if configured else [],
        "limits": limits_block(facts),
        "readiness": rows_,
        "ready": is_ready(rows_),
        "readiness_sha256": digest(rows_, facts),
        "prepared": facts.get("prepared") if configured else None,
        "live_service": live_block(facts, enabled) if full else None,
        "leftovers": leftovers_block(facts, enabled) if full else None,
        "confirm": dict(CONFIRM),
        "notices": [{"code": code, "text": text} for code, text in NOTICES.items()],
    }


# --- the reader: cache, single flight -------------------------------------------------------


class Readiness:
    """Reads the readiness and keeps the document of ``GET /aeri`` for
    ``CACHE_S``. One read at a time; callers that arrive meanwhile wait for it.
    The age of a cached read counts from when it **began**, so a slow read is not
    served for longer than a quick one."""

    def __init__(self, host: Host | None = None):
        self.host = host or production_host()
        self._flight = threading.Lock()
        self._cache: tuple | None = None  # (monotonic time the read began, facts)
        self.reads = 0

    def facts(self, *, fresh=False) -> dict:
        with self._flight:
            mono = self.host.monotonic
            cached = self._cache
            if not fresh and cached is not None and 0 <= mono() - cached[0] < CACHE_S:
                return cached[1]
            began = mono()
            facts = collect(self.host)
            self.reads += 1
            self._cache = (began, facts)
            return facts

    def forget(self) -> None:
        with self._flight:
            self._cache = None

    def view(self) -> dict:
        """The document, from a read at most ``CACHE_S`` old."""
        return document(self.facts())

    def snapshot(self, *, fresh=True) -> dict:
        """``{"rows", "ready", "sha256", "sampled_at", "prepared"}`` for the
        hosted run's challenge and start: ``fresh`` (the default) never answers
        from the cache."""
        facts = self.facts(fresh=fresh)
        rows_ = rows(facts)
        prepared = facts.get("prepared")
        return {
            "rows": [
                {"code": r["code"], "level": r["level"], "params": r["params"]}
                for r in rows_
            ],
            "ready": is_ready(rows_),
            "sha256": digest(rows_, facts),
            "sampled_at": record.iso(facts["sampled_at"]),
            "prepared": prepared,
        }


_READINESS: list = []
_LOCK = threading.Lock()


def readiness() -> Readiness:
    """The process's one reader (tests replace this function)."""
    with _LOCK:
        if not _READINESS:
            _READINESS.append(Readiness())
        return _READINESS[0]


def snapshot(host=None, *, fresh=True) -> dict:
    """The interface of the hosted run (A1): ``aeri_ready.snapshot(host)``. With
    no ``host`` it is the process's reader; a ``Host`` reads once and keeps
    nothing."""
    reader = readiness() if host is None else Readiness(host)
    return reader.snapshot(fresh=fresh)
