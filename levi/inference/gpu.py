"""GPU guardian: the local model uses the GPU only when it harms nobody.

On a shared machine the GPU also runs other people's work -- a real-robot RL
actor, a learner, a notebook. The guardian decides, before every local-model
request and continuously while the service runs, whether the local model may
use the GPU now, and says why and for how long it waits.

What it knows, from sampling ``nvidia-smi`` (no probing of anyone's process):

- **Who is on the GPU.** Every compute process that is not Ollama's own is
  identified by a *signature*: its command without paths or run-specific
  arguments, so an actor restarted with a new checkpoint is still the same
  workload. Each signature's appearances are kept as intervals.
- **What kind of work it is.** A policy (``models/gpu-policy.json``) classes
  signatures as ``protect`` (never share: latency-sensitive work such as a
  robot actor or a policy server), ``share`` (may coexist when memory and
  utilisation leave room) or ``ignore``. Anything unknown is ``protect``.
- **How it comes and goes.** Work that restarts in rounds leaves short gaps.
  After a protected workload leaves, the guardian waits longer than the gaps it
  has seen that workload leave before coming back (1.5 x their 90th
  percentile, between 60 s and 30 min); without enough history, it waits
  ``LEVI_GPU_QUIET_SECONDS`` (300 s). A session that has really ended opens
  the GPU sooner than a fixed timer would, and one that pauses between rounds
  does not get interrupted.

What it does:

- ``require_free`` raises ``GpuBusy`` with the reason and the expected wait;
  the run stops at that boundary and is marked ``blocked_by: gpu``.
- ``Watch`` samples every 15 s, every 2 s while a local model is resident.
  When a protected workload appears, resident models are unloaded at once and
  any request in flight fails as preempted; its phase resumes later from the
  cache. When the GPU opens again, runs blocked by it are resumed.

``LEVI_GPU_SHARING=allow`` switches all of this off for a machine where the
people involved have agreed to share.
"""

import itertools
import json
import logging
import os
import re
import shutil
import statistics
import subprocess
import threading
import time
from pathlib import Path

LOG = logging.getLogger("levi")

DEFAULT_QUIET = float(os.getenv("LEVI_GPU_QUIET_SECONDS", "300"))
# Kept for callers of the earlier fixed-window guard.
QUIET_SECONDS = DEFAULT_QUIET
MIN_QUIET, MAX_QUIET = 60.0, 1800.0
# Gaps longer than this are separate sessions, not restarts within one.
SESSION_GAP = 3600.0
HISTORY_PER_SIGNATURE = 60
DEFAULT_POLICY = {
    "default": "protect",
    "rules": [],
    "share": {"max_utilization": 30, "margin_mib": 1024},
}


class GpuBusy(ValueError):
    pass


# --- sampling -------------------------------------------------------------


def _cmdline(pid):
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode(errors="replace").strip()


def _parent(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[1])
    except (OSError, IndexError, ValueError):
        return None


def _is_ollama(pid, command):
    """Ollama's own model process: `ollama runner` in older releases,
    `llama-server` started by `ollama serve` in newer ones. Recognised by
    being Ollama's binary, living in Ollama's install tree, or having an
    `ollama` parent -- never by a name alone that anyone could reuse."""
    program = command.split(" ", 1)[0] if command else ""
    if os.path.basename(program).startswith("ollama"):
        return True
    if "/ollama" in program and "/lib/" in program:
        return True
    parent = _parent(pid) if pid else None
    if parent:
        parent_program = _cmdline(parent).split(" ", 1)[0]
        return os.path.basename(parent_program).startswith("ollama")
    return False


def signature(command):
    """The workload behind a command, stable across its restarts.

    Paths and path-valued options (checkpoints, run directories) change from
    one restart to the next; the program, module and plain flags do not.
    """
    tokens = []
    for token in command.split():
        if "/" in token and not token.startswith("-"):
            token = os.path.basename(token)
        elif "=" in token and "/" in token.split("=", 1)[1]:
            continue
        tokens.append(token)
    return " ".join(tokens[:8]) or "unknown"


def _smi(query, fields):
    out = subprocess.run(
        ["nvidia-smi", f"--query-{query}={fields}", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout
    return [[p.strip() for p in line.split(",")] for line in out.strip().splitlines()]


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def sample():
    """One look at the GPU. ``available`` is False when there is no NVIDIA GPU;
    ``error`` is set when the GPU exists but could not be read."""
    if not shutil.which("nvidia-smi"):
        return {"available": False, "at": time.time(), "processes": [], "ours": []}
    try:
        apps = _smi("compute-apps", "pid,process_name,used_memory")
        gpus = _smi("gpu", "memory.total,memory.used,utilization.gpu")
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "available": True,
            "at": time.time(),
            "error": f"nvidia-smi unreadable: {type(exc).__name__}",
            "processes": [],
            "ours": [],
        }
    processes, ours = [], []
    for parts in apps:
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        command = _cmdline(pid) or parts[1]
        row = {"pid": pid, "command": command[:200], "memory_mib": _int(parts[2])}
        if _is_ollama(pid, command):
            ours.append(row)
            continue
        row["signature"] = signature(command)
        processes.append(row)
    total = sum(_int(g[0]) or 0 for g in gpus if len(g) >= 3)
    used = sum(_int(g[1]) or 0 for g in gpus if len(g) >= 3)
    utils = [_int(g[2]) for g in gpus if len(g) >= 3 and _int(g[2]) is not None]
    return {
        "available": True,
        "at": time.time(),
        "processes": processes,
        "ours": ours,
        "memory_total_mib": total,
        "memory_used_mib": used,
        "utilization": max(utils) if utils else None,
    }


def others():
    """Compute processes on the GPU that are not Ollama's (compatibility)."""
    now = sample()
    if now.get("error"):
        return [{"pid": None, "command": now["error"], "memory_mib": None}]
    return now["processes"]


# --- memory of who used the GPU -------------------------------------------


def _state_dir():
    from levi.paths import STATE

    return STATE / "models"


def _history_path():
    return _state_dir() / "ollama" / "gpu-history.json"


def _policy_path():
    return _state_dir() / "gpu-policy.json"


def _read(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}-{threading.get_ident()}.partial"
    )
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1))
    os.replace(temporary, path)


_HISTORY_LOCK = threading.Lock()


def record(now, history=None):
    """Fold one sample into the per-signature appearance intervals."""
    with _HISTORY_LOCK:
        history = history if history is not None else _read(_history_path(), {})
        workloads = history.setdefault("workloads", {})
        present = {row["signature"]: row for row in now.get("processes", [])}
        at = now["at"]
        for sig, row in present.items():
            entry = workloads.setdefault(
                sig, {"intervals": [], "example": row["command"]}
            )
            entry["example"] = row["command"]
            entry["memory_mib"] = row.get("memory_mib")
            intervals = entry["intervals"]
            if intervals and intervals[-1][1] is None:
                continue  # still running
            intervals.append([at, None])
            del intervals[:-HISTORY_PER_SIGNATURE]
        for sig, entry in workloads.items():
            intervals = entry["intervals"]
            if sig not in present and intervals and intervals[-1][1] is None:
                intervals[-1][1] = at
        if now.get("ours"):
            history["ours_memory_mib"] = max(
                r.get("memory_mib") or 0 for r in now["ours"]
            )
        history["last_sample"] = {
            k: now.get(k)
            for k in ("at", "utilization", "memory_used_mib", "memory_total_mib")
        }
        utils = history.setdefault("utilization", [])
        if now.get("utilization") is not None:
            utils.append([at, now["utilization"]])
            del utils[:-40]
        _write(_history_path(), history)
        return history


def learned_quiet(entry):
    """How long to wait after this workload leaves before assuming it is done."""
    gaps = []
    intervals = entry.get("intervals", [])
    for previous, following in itertools.pairwise(intervals):
        if previous[1] is None:
            continue
        gap = following[0] - previous[1]
        if 0 < gap < SESSION_GAP:
            gaps.append(gap)
    if len(gaps) < 2:
        return DEFAULT_QUIET, {"basis": "default", "restarts_seen": len(gaps)}
    gaps.sort()
    p90 = gaps[min(len(gaps) - 1, round(0.9 * (len(gaps) - 1)))]
    quiet = min(MAX_QUIET, max(MIN_QUIET, 1.5 * p90))
    return quiet, {
        "basis": "learned",
        "restarts_seen": len(gaps),
        "gap_p90_seconds": round(p90, 1),
        "gap_median_seconds": round(statistics.median(gaps), 1),
    }


# --- policy -----------------------------------------------------------------


def policy():
    value = _read(_policy_path(), None) or {}
    merged = {**DEFAULT_POLICY, **value}
    merged["share"] = {**DEFAULT_POLICY["share"], **(value.get("share") or {})}
    return merged


def classify(sig, rules=None):
    rules = policy() if rules is None else rules
    for rule in rules.get("rules", []):
        try:
            if re.search(rule["match"], sig):
                return rule.get("class", "protect")
        except (re.error, KeyError):
            continue
    return rules.get("default", "protect")


# --- the decision -------------------------------------------------------------


def decide(now, history, rules, need_mib=None, at=None):
    """``{"state": free|busy|cooling|shared|unknown, "reason", "wait_seconds", …}``.

    Pure: everything it uses is passed in, so it can be tested and explained.
    """
    at = at or now["at"]
    if not now.get("available"):
        return {
            "state": "free",
            "reason": "No NVIDIA GPU on this machine.",
            "wait_seconds": 0,
        }
    if now.get("error"):
        return {
            "state": "unknown",
            "reason": f"{now['error']}; the local model waits rather than guess.",
            "wait_seconds": 60,
        }
    need = need_mib or history.get("ours_memory_mib") or 6000
    share = rules["share"]
    recent = [u for t, u in history.get("utilization", []) if at - t <= 60]
    utilization = max(recent) if recent else now.get("utilization")
    free_mib = (now.get("memory_total_mib") or 0) - (now.get("memory_used_mib") or 0)
    sharing = []
    for row in now["processes"]:
        kind = classify(row["signature"], rules)
        if kind == "ignore":
            continue
        if kind == "share":
            room = free_mib >= need + share["margin_mib"] or bool(now.get("ours"))
            quiet = utilization is None or utilization <= share["max_utilization"]
            if room and quiet:
                sharing.append(row)
                continue
            why = "not enough free memory" if not room else f"GPU {utilization}% busy"
            return {
                "state": "busy",
                "reason": f"{row['signature']} (pid {row['pid']}, share) is on the GPU and {why}.",
                "who": row,
                "wait_seconds": None,
            }
        return {
            "state": "busy",
            "reason": f"{row['signature']} (pid {row['pid']}, {row.get('memory_mib')} MiB) "
            "is on the GPU and is protected.",
            "who": row,
            "class": kind,
            "wait_seconds": None,
        }
    waits = []
    for sig, entry in history.get("workloads", {}).items():
        if classify(sig, rules) != "protect":
            continue
        intervals = entry.get("intervals") or []
        if not intervals or intervals[-1][1] is None:
            continue
        left = at - intervals[-1][1]
        quiet, basis = learned_quiet(entry)
        if left < quiet:
            waits.append((quiet - left, sig, left, quiet, basis))
    if waits:
        wait, sig, left, quiet, basis = max(waits)
        how = (
            f"learned from {basis['restarts_seen']} restarts (90% of its gaps "
            f"≤ {basis['gap_p90_seconds']} s)"
            if basis["basis"] == "learned"
            else "default window"
        )
        return {
            "state": "cooling",
            "reason": f"{sig} left the GPU {int(left)} s ago; waiting {int(wait)} s "
            f"more in case it comes back ({int(quiet)} s window, {how}).",
            "wait_seconds": int(wait) + 1,
            "window": basis | {"quiet_seconds": int(quiet)},
        }
    if sharing:
        return {
            "state": "shared",
            "reason": "Sharing with "
            + ", ".join(r["signature"] for r in sharing)
            + " by policy; memory and utilisation leave room.",
            "wait_seconds": 0,
        }
    return {
        "state": "free",
        "reason": "No other compute on the GPU.",
        "wait_seconds": 0,
    }


def status(need_mib=None):
    """Sample, remember and decide: what the guardian thinks right now."""
    now = sample()
    history = (
        record(now)
        if now.get("available") and not now.get("error")
        else _read(_history_path(), {})
    )
    verdict = decide(now, history, policy(), need_mib)
    verdict["sampled_at"] = now["at"]
    verdict["processes"] = now.get("processes", [])
    verdict["ours"] = now.get("ours", [])
    return verdict


def local_endpoint(base_url):
    return any(h in (base_url or "") for h in ("127.0.0.1", "localhost", "[::1]"))


def require_free(config=None):
    """Refuse a local model call (or starting the local service, ``config``
    None) unless the guardian says the GPU is free or shareable."""
    if os.getenv("LEVI_GPU_SHARING") == "allow":
        return None
    if config is not None and not local_endpoint(config.base_url):
        return None
    verdict = status()
    if verdict["state"] in {"free", "shared"}:
        return verdict
    wait = verdict.get("wait_seconds")
    raise GpuBusy(
        f"GPU guardian ({verdict['state']}): {verdict['reason']}"
        + (f" Expected wait: about {wait} s." if wait else "")
        + " The run resumes automatically when the GPU is clear."
    )


# Compatibility for callers and tests of the fixed-window guard.
def _seen_path():
    return _state_dir() / "ollama" / "gpu-last-busy.json"


def mark_busy(rows):
    _write(_seen_path(), {"at": time.time(), "processes": rows})


def last_busy():
    return _read(_seen_path(), None)


# --- acting on it -------------------------------------------------------------


def unload_all(config):
    """Ask the local Ollama service to drop every resident model."""
    from .provider import client_for

    client = client_for(config, timeout=30)
    try:
        running = client._request("GET", "/api/ps").get("models") or []
    except Exception:  # noqa: BLE001 - nothing to unload if unreachable
        return []
    dropped = []
    for model in running:
        try:
            client._request(
                "POST",
                "/api/generate",
                {"model": model["name"], "keep_alive": 0, "prompt": ""},
            )
            dropped.append(model["name"])
        except Exception:  # noqa: BLE001 - try the rest
            LOG.warning("could not unload %s", model.get("name"))
    return dropped


class Watch:
    """The guardian's loop inside the service."""

    IDLE_INTERVAL = 15.0
    ACTIVE_INTERVAL = 2.0

    def __init__(self, store, interval=None, workbench=None):
        self.store = store
        self.workbench = workbench
        self.fixed = interval
        self.stop = threading.Event()
        self.thread = None
        self.last = None

    def configs(self):
        from levi.agent.schema import ProviderConfig

        for row in self.store.list("providers"):
            try:
                config = ProviderConfig.model_validate(row)
            except ValueError:
                continue
            if (
                config.kind == "ollama"
                and config.enabled
                and local_endpoint(config.base_url)
            ):
                yield config

    def resume_blocked(self):
        """Runs the guardian stopped go on once the GPU is clear, one per tick."""
        if self.workbench is None:
            return None
        from levi.agent.runtime import _ACTIVE

        for run in self.store.list("runs"):
            if run.get("status") != "blocked" or run.get("blocked_by") != "gpu":
                continue
            if run["id"] in _ACTIVE:
                continue
            plan = run.get("plan") or {}
            accepted = (plan.get("pilot_review") or {}).get("accepted")
            done = set(run.get("completed", []))
            if not accepted and plan.get("pilot_episode") in done:
                continue  # waiting for a person to review the pilot, not for the GPU
            try:
                self.workbench.launch(run["id"], pilot=not accepted)
                return run["id"]
            except Exception as exc:  # noqa: BLE001 - leave it blocked, say why
                LOG.warning("could not resume %s: %s", run["id"], exc)
        return None

    def tick(self):
        if os.getenv("LEVI_GPU_SHARING") == "allow":
            return None
        verdict = status()
        self.last = verdict
        if verdict["state"] in {"busy", "unknown"} and verdict.get("ours") is not None:
            dropped = []
            for config in self.configs():
                dropped += unload_all(config)
            if dropped:
                verdict["unloaded"] = dropped
                LOG.info("GPU guardian unloaded %s: %s", dropped, verdict["reason"])
        elif verdict["state"] in {"free", "shared"}:
            resumed = self.resume_blocked()
            if resumed:
                verdict["resumed"] = resumed
        return verdict

    def interval(self):
        if self.fixed:
            return self.fixed
        busy_with_us = self.last and self.last.get("ours")
        return self.ACTIVE_INTERVAL if busy_with_us else self.IDLE_INTERVAL

    def run(self):
        while not self.stop.wait(self.interval()):
            try:
                self.tick()
            except Exception:
                LOG.exception("GPU guardian tick failed")

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)


def report():
    """What the guardian decides now, and what it has learned about each workload."""
    verdict = status()
    history = _read(_history_path(), {})
    rules = policy()
    workloads = []
    for sig, entry in sorted(history.get("workloads", {}).items()):
        intervals = entry.get("intervals") or []
        quiet, basis = learned_quiet(entry)
        workloads.append(
            {
                "signature": sig,
                "class": classify(sig, rules),
                "running": bool(intervals and intervals[-1][1] is None),
                "appearances": len(intervals),
                "last_left_at": intervals[-1][1] if intervals else None,
                "quiet_seconds": int(quiet),
                "window": basis,
                "memory_mib": entry.get("memory_mib"),
            }
        )
    return {
        "decision": {
            k: v for k, v in verdict.items() if k not in {"processes", "ours"}
        },
        "on_gpu": verdict.get("processes", []),
        "local_model_resident": bool(verdict.get("ours")),
        "local_model_memory_mib": history.get("ours_memory_mib"),
        "workloads": workloads,
        "policy": rules,
        "policy_file": str(_policy_path()),
        "sharing_override": os.getenv("LEVI_GPU_SHARING") == "allow",
    }
