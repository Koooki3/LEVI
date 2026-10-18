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
- ``passive_ready(arm)``: **reads only**: the unit is active, every socket
  listening on the arm's port is held by a process of that unit, and the
  unit was started with the arm's config and checkpoint (its ``ExecStart``
  as systemd recorded it). Listeners are found through ``/proc/net/tcp{,6}``;
  their owners only among the pids in the unit's own ``cgroup.procs``
  (``systemctl --user show -p ControlGroup``), whose ``/proc/<pid>/fd`` are
  read. ``/proc`` is never listed and no other process is opened (root
  AGENTS.md execution boundary, user 2026-10-14).
  Nothing here opens a socket: the policy port (8000 on the robot machine)
  is never connected to; the real handshake is the child run's PREFLIGHT.

A listener not held by our unit (another person's process, or an owner that
cannot be read) counts as a mismatch, and its pid is never looked up:
unknown is never ready.
"""

import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .conductor import ArmRef, HostResult, Readiness

LISTEN = "0A"
PROC_NET = ("net/tcp", "net/tcp6")
WHITESPACE = re.compile(r"\s")


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
    # Processes of OUR units (their cgroup.procs) holding the socket. Empty: not
    # ours, a foreign or unknown owner (never looked up; we do not open other
    # people's /proc entries). One holder of ours is enough to count the socket
    # as ours: a foreign process sharing the same socket (an inherited fd) is
    # not seen, which is looser than the old whole-/proc scan. We accept that:
    # telling it apart would mean opening other people's processes, and such a
    # sharer could only arise from a process our unit itself spawned.
    pids: tuple
    # When ``pids`` is empty: the units asked about whose own cgroup could not
    # be read, so the owner is unknown rather than known to be foreign.
    unknown: tuple = ()

    def not_ours(self) -> str:
        """Why this listener is not ours, for an operator."""
        if self.unknown:
            return (
                f"port {self.port}: owner unknown: "
                + ", ".join(self.unknown)
                + " cgroup unreadable"
            )
        return f"port {self.port} is held by a process that is not ours"


CGROUP_ROOT = "/sys/fs/cgroup"


class ProcView:
    """Read-only view of this run's own processes: the kernel-wide socket tables
    and the ``fd`` entries of the pids in our own units' ``cgroup.procs`` (from
    ``systemctl --user show -p ControlGroup``). It never lists ``/proc`` and never
    opens another person's ``/proc/<pid>`` (root AGENTS.md execution boundary,
    user 2026-10-14). ``root``/``cgroup_root`` are fake trees in tests."""

    def __init__(self, root=None, *, systemd=None, cgroup_root=CGROUP_ROOT):
        # None: the real kernel paths, built one entry at a time (never a
        # "/proc" root that could be listed).
        self.root = None if root is None else Path(root)
        self.systemd = systemd or UserSystemd()
        self.cgroup_root = str(cgroup_root)

    def path(self, *parts) -> Path:
        """One entry: ("net/tcp",) or (pid, "fd")."""
        rel = "/".join(str(p) for p in parts)
        return Path(f"/proc/{rel}") if self.root is None else self.root / rel

    def listening_inodes(self, port: int) -> list[int]:
        found = []
        for name in PROC_NET:
            try:
                lines = self.path(name).read_text().splitlines()[1:]
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

    def unit_pids(self, unit: str) -> list[int]:
        """The pids in our unit's ``cgroup.procs`` ([] when the unit has no
        cgroup: nothing runs in it). Raises ``OSError`` when it cannot be read.
        Only the top-level ``cgroup.procs`` is read: no start command of ours
        delegates the cgroup (``Delegate=``), so the unit's processes stay in it."""
        code, out = self.systemd.run(
            [
                "systemctl",
                "--user",
                "show",
                "-p",
                "ControlGroup",
                "--value",
                f"{unit}.service",
            ]
        )
        if code != 0:
            raise OSError(f"systemctl show {unit}: exit {code}")
        group = out.strip()
        if not group:
            return []
        if not group.startswith("/") or ".." in group.split("/"):
            raise OSError(f"unexpected control group of {unit}: {group!r}")
        text = Path(self.cgroup_root + group + "/cgroup.procs").read_text()
        try:
            return sorted({int(x) for x in text.split()})
        except ValueError:
            raise OSError(f"malformed cgroup.procs of {unit}") from None

    def owners(self, inodes, units) -> tuple[dict[int, list[int]], tuple]:
        """For each socket inode, the processes of ``units`` that hold it, and
        the units whose cgroup could not be read. Such a unit owns nothing
        (fail closed: not ours)."""
        wanted = {f"socket:[{inode}]": inode for inode in inodes}
        out: dict[int, list[int]] = {inode: [] for inode in inodes}
        unreadable: list[str] = []
        if not wanted:
            return out, ()
        for unit in units:
            try:
                pids = self.unit_pids(unit)
            except OSError:
                unreadable.append(unit)
                continue
            for pid in pids:
                try:
                    entries = list(self.path(pid, "fd").iterdir())
                except OSError:
                    continue  # gone
                for entry in entries:
                    try:
                        target = os.readlink(entry)
                    except OSError:
                        continue
                    if target in wanted:
                        out[wanted[target]].append(pid)
        return out, tuple(unreadable)

    def listeners(self, port: int, units=()) -> list[Listener]:
        """Every listening socket on ``port``; ``pids`` are its holders among
        ``units``' processes (empty: not ours; ``unknown`` names the units
        whose cgroup could not be read)."""
        inodes = self.listening_inodes(port)
        owners, unreadable = self.owners(inodes, units)
        out = []
        for i in inodes:
            pids = tuple(sorted(set(owners[i])))
            out.append(Listener(port, i, pids, () if pids else unreadable))
        return out


def exec_argv(text: str) -> list[str]:
    """The argv of ``systemctl show -p ExecStart --value`` (what systemd-run was
    given for our unit: our own record, not the process's /proc cmdline)."""
    _, sep, rest = text.partition("argv[]=")
    if not sep:
        return []
    argv, _, _ = rest.partition(" ; ")
    return argv.split()


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
        proc_root=None,
        cgroup_root=CGROUP_ROOT,
        recipe: Callable[[ArmRef], list] | None = None,
        stop_timeout_s: float = 60.0,
        poll_s: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.systemd = systemd or UserSystemd()
        self.proc = ProcView(proc_root, systemd=self.systemd, cgroup_root=cgroup_root)
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

    def runs(self, arm: ArmRef) -> bool:
        """Our unit was started with the arm's config and checkpoint (systemd's
        ExecStart record of the unit)."""
        code, out = self.systemd.run(
            [
                "systemctl",
                "--user",
                "show",
                "-p",
                "ExecStart",
                "--value",
                f"{arm.unit}.service",
            ]
        )
        return code == 0 and runs_arm(exec_argv(out), arm)

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
            left = [lsn for port in ports for lsn in self.proc.listeners(port, units)]
            foreign = [lsn for lsn in left if not lsn.pids]
            if foreign:
                return HostResult(
                    "yes",
                    "foreign_listener",
                    mismatch=True,
                    detail=foreign[0].not_ours(),
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
        held = self.proc.listeners(arm.port, (arm.unit,))
        if held:
            ours = all(lsn.pids for lsn in held) and self.runs(arm)
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
        if any(not isinstance(a, str) or WHITESPACE.search(a) for a in command):
            # systemd records ExecStart space-separated and passive_ready
            # splits it on whitespace: such a unit could never be ours.
            return HostResult(
                "no",
                "recipe_unsafe",
                detail="the launch recipe has an argument with whitespace, which "
                "the passive identity check (ExecStart) cannot tell apart",
            )
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
        held = self.proc.listeners(arm.port, (arm.unit,))
        if held:
            for listener in held:
                if not listener.pids:
                    return Readiness("mismatch", listener.not_ours())
            if not self.runs(arm):
                return Readiness(
                    "mismatch",
                    f"{arm.unit} does not run {arm.config} with "
                    f"{Path(arm.checkpoint_dir).name}",
                )
        if state != "active":
            return Readiness("not_ready", f"unit {state}")
        if not held:
            return Readiness("not_ready", f"nothing listens on {arm.port} yet")
        return Readiness("ready")
