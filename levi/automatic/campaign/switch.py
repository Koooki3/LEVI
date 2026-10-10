"""Switching the policy service between arms (design X3 §1.6, T-CP-04).

``SystemdPolicyHost`` is the conductor's ``PolicyHost``. Each arm's policy
service is a transient user unit ``levi-policy-<campaign>-<arm>.service``:

- ``stop(arms)``: stops every active unit of the campaign, then waits until
  nobody listens on their policy ports. A listener that remains and is not
  one of the campaign's units (for example a policy server started by hand
  in a terminal) is a mismatch: the conductor locks the campaign
  (``FAULT_LOCKED``) instead of taking the port over.
- ``start(arm)``: ``systemd-run --user --unit=<unit> -- <recipe>``. The
  command comes from an injected recipe (the launch recipes of the setup
  registry, T-SU); without one nothing is started (``no_recipe``). A port
  already held by another process is a mismatch.
- ``passive_ready(arm)``: **reads only**: the unit is active, and every
  process listening on the arm's port belongs to that unit
  (``/proc/<pid>/cgroup``) and runs the arm's config and checkpoint
  (``/proc/<pid>/cmdline``). Listeners are found through
  ``/proc/net/tcp{,6}`` and the socket inodes under ``/proc/<pid>/fd``.
  Nothing here opens a socket: the policy port (8000 on the robot machine)
  is never connected to; the real handshake is the child run's PREFLIGHT.

A listener whose owner cannot be read (another user's process) counts as a
mismatch: unknown is never ready.
"""

import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .conductor import ArmRef, HostResult, Readiness

LISTEN = "0A"
PROC_NET = ("net/tcp", "net/tcp6")


class Systemd(Protocol):
    """Runs a ``systemctl --user`` or ``systemd-run --user`` command:
    ``(returncode, stdout)``."""

    def run(self, argv: list) -> tuple[int, str]: ...


class UserSystemd:
    """The real runner (never used by the tests)."""

    def __init__(self, timeout_s: float = 60.0):
        self.timeout_s = timeout_s

    def run(self, argv: list) -> tuple[int, str]:
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 125, str(exc)
        return done.returncode, done.stdout


# --- reading /proc ---------------------------------------------------------------------


@dataclass(frozen=True)
class Listener:
    port: int
    inode: int
    pids: tuple  # every process holding the socket (empty: owner unknown)


class ProcView:
    """Read-only view of ``/proc`` (a fake tree in tests)."""

    def __init__(self, root="/proc"):
        self.root = Path(root)

    def listening_inodes(self, port: int) -> list[int]:
        found = []
        for name in PROC_NET:
            try:
                lines = (self.root / name).read_text().splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                fields = line.split()
                if len(fields) < 10 or fields[3] != LISTEN:
                    continue
                try:
                    local_port = int(fields[1].rsplit(":", 1)[1], 16)
                    inode = int(fields[9])
                except (IndexError, ValueError):
                    continue
                if local_port == port and inode:
                    found.append(inode)
        return sorted(set(found))

    def _pids(self) -> list[int]:
        try:
            return sorted(int(p.name) for p in self.root.iterdir() if p.name.isdigit())
        except OSError:
            return []

    def owners(self, inodes) -> dict[int, list[int]]:
        wanted = {f"socket:[{inode}]": inode for inode in inodes}
        out: dict[int, list[int]] = {inode: [] for inode in inodes}
        if not wanted:
            return out
        for pid in self._pids():
            folder = self.root / str(pid) / "fd"
            try:
                entries = list(folder.iterdir())
            except OSError:
                continue  # gone, or not ours to read
            for entry in entries:
                try:
                    target = os.readlink(entry)
                except OSError:
                    continue
                if target in wanted:
                    out[wanted[target]].append(pid)
        return out

    def listeners(self, port: int) -> list[Listener]:
        inodes = self.listening_inodes(port)
        owners = self.owners(inodes)
        return [Listener(port, i, tuple(sorted(set(owners[i])))) for i in inodes]

    def cgroup(self, pid: int) -> str:
        try:
            return (self.root / str(pid) / "cgroup").read_text()
        except OSError:
            return ""

    def cmdline(self, pid: int) -> list[str]:
        try:
            raw = (self.root / str(pid) / "cmdline").read_bytes()
        except OSError:
            return []
        return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def in_unit(cgroup: str, unit: str) -> bool:
    """Whether a process's cgroup is the unit's (or below it)."""
    name = f"/{unit}.service"
    for line in cgroup.splitlines():
        path = line.split(":", 2)[-1]
        if path.endswith(name) or f"{name}/" in path:
            return True
    return False


def _values(argv: list[str]) -> list[str]:
    out = []
    for arg in argv:
        out.append(arg)
        if arg.startswith("-") and "=" in arg:
            out.append(arg.split("=", 1)[1])
    return out


def runs_arm(argv: list[str], arm: ArmRef) -> bool:
    """The command line names the arm's config (as a whole word: the plain
    config is a prefix of the CFG one) and its checkpoint folder."""
    values = _values(argv)
    if arm.config not in values:
        return False
    wanted = arm.checkpoint_dir.rstrip("/")
    name = Path(wanted).name
    for value in values:
        value = value.rstrip("/")
        if not value or Path(value).name != name:
            continue
        if value == wanted or (
            not value.startswith("/") and wanted.endswith("/" + value)
        ):
            return True
        if value == name:
            return True
    return False


# --- the host --------------------------------------------------------------------------


class SystemdPolicyHost:
    def __init__(
        self,
        *,
        systemd: Systemd | None = None,
        proc_root="/proc",
        recipe: Callable[[ArmRef], list] | None = None,
        stop_timeout_s: float = 60.0,
        poll_s: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.systemd = systemd or UserSystemd()
        self.proc = ProcView(proc_root)
        self.recipe = recipe
        self.stop_timeout_s = stop_timeout_s
        self.poll_s = poll_s
        self._clock = clock
        self._sleep = sleep

    def unit_state(self, unit: str) -> str:
        code, out = self.systemd.run(
            [
                "systemctl",
                "--user",
                "show",
                "-p",
                "ActiveState",
                "--value",
                f"{unit}.service",
            ]
        )
        return out.strip() if code == 0 and out.strip() else "unknown"

    def _ours(self, listener: Listener, units) -> bool:
        return bool(listener.pids) and all(
            any(in_unit(self.proc.cgroup(pid), unit) for unit in units)
            for pid in listener.pids
        )

    def stop(self, arms: list) -> HostResult:
        units = [arm.unit for arm in arms]
        failed = []
        for unit in units:
            if self.unit_state(unit) in ("inactive", "failed", "unknown"):
                continue
            code, _ = self.systemd.run(
                ["systemctl", "--user", "stop", f"{unit}.service"]
            )
            if code != 0:
                failed.append(unit)
        ports = sorted({arm.port for arm in arms})
        deadline = self._clock() + self.stop_timeout_s
        while True:
            left = [lsn for port in ports for lsn in self.proc.listeners(port)]
            foreign = [lsn for lsn in left if not self._ours(lsn, units)]
            if foreign:
                pids = sorted({p for lsn in foreign for p in lsn.pids})
                return HostResult(
                    "yes",
                    "foreign_listener",
                    mismatch=True,
                    detail=f"port {foreign[0].port} held by pids {pids or 'unknown'}",
                )
            if not left:
                break
            if self._clock() >= deadline:
                return HostResult("unknown", "still_listening", detail=str(units))
            self._sleep(self.poll_s)
        if failed:
            return HostResult("no", "stop_failed", detail=", ".join(failed))
        return HostResult("yes", "stopped")

    def start(self, arm: ArmRef) -> HostResult:
        held = self.proc.listeners(arm.port)
        if held:
            ours = all(
                in_unit(self.proc.cgroup(pid), arm.unit)
                and runs_arm(self.proc.cmdline(pid), arm)
                for lsn in held
                for pid in lsn.pids
            ) and all(lsn.pids for lsn in held)
            if ours and self.unit_state(arm.unit) == "active":
                return HostResult("yes", "already_running")
            return HostResult(
                "yes", "port_taken", mismatch=True, detail=f"port {arm.port} is held"
            )
        if self.recipe is None:
            return HostResult("no", "no_recipe", detail="no launch recipe for policies")
        command = list(self.recipe(arm))
        if not command:
            return HostResult("no", "no_recipe")
        code, _ = self.systemd.run(
            [
                "systemd-run",
                "--user",
                f"--unit={arm.unit}",
                "--collect",
                "--property=KillMode=control-group",
                "--",
                *command,
            ]
        )
        if code != 0:
            return HostResult("no", "start_failed", detail=f"systemd-run exit {code}")
        return HostResult("yes", "started")

    def passive_ready(self, arm: ArmRef) -> Readiness:
        state = self.unit_state(arm.unit)
        held = self.proc.listeners(arm.port)
        if held:
            for listener in held:
                if not listener.pids:
                    return Readiness("mismatch", f"port {arm.port}: owner unknown")
                for pid in listener.pids:
                    if not in_unit(self.proc.cgroup(pid), arm.unit):
                        return Readiness(
                            "mismatch", f"port {arm.port}: pid {pid} is not {arm.unit}"
                        )
                    if not runs_arm(self.proc.cmdline(pid), arm):
                        return Readiness(
                            "mismatch",
                            f"pid {pid} does not run {arm.config} with "
                            f"{Path(arm.checkpoint_dir).name}",
                        )
        if state != "active":
            return Readiness("not_ready", f"unit {state}")
        if not held:
            return Readiness("not_ready", f"nothing listens on {arm.port} yet")
        return Readiness("ready")
