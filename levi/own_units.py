"""Which processes are ours, decided only from our own registry.

The machine is shared (root AGENTS.md execution boundary, user 2026-10-14):
whether a GPU process belongs to us is never read from that process's
``/proc/<pid>`` (no fd, cwd, cmdline or stat of a pid that is not ours). The
only evidence is the ``cgroup.procs`` of our own systemd user units:

* our policy servers: the ``levi-aeri-policy-*`` units;
* LEVI's own model server: the ``levi-live`` unit (vLLM runs inside it).

A pid listed there is ours; any other pid is someone else's, and nothing more
is read about it. Linking a port to one of our units reads only the kernel's
global socket tables (``/proc/net/tcp``, ``/proc/net/tcp6``) and the ``fd``
entries of our own pids. Whatever cannot be read is not ours (fail closed).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

CGROUP_ROOT = "/sys/fs/cgroup"
LISTEN = "0A"
NET_TABLES = ("net/tcp", "net/tcp6")
# The names of our policy units (``levi-aeri-policy-<name>``): the services
# fr3_policy_swap starts (it imports this) and the scopes a hand-started policy
# server runs in (``systemd-run --user --scope --unit
# levi-aeri-policy-manual-<name>``, setup.md).
POLICY_UNIT = re.compile(r"levi-aeri-policy-[a-z0-9][a-z0-9-]{0,46}")
POLICY_UNIT_FILE = re.compile(POLICY_UNIT.pattern + r"\.(?:service|scope)")


def _systemctl(argv: list) -> tuple[int, str]:
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        # ValueError: output that is not text (UnicodeDecodeError).
        return 125, str(exc)
    return done.returncode, done.stdout


class OwnUnits:
    """Read-only view of our own units. ``root`` is a fake kernel tree and
    ``cgroup_root``/``systemctl`` fakes in tests; None: the real paths, built
    one entry at a time (this never lists a process directory)."""

    def __init__(
        self,
        root=None,
        *,
        cgroup_root=CGROUP_ROOT,
        systemctl: Callable[[list], tuple[int, str]] | None = None,
    ):
        self.root = None if root is None else Path(root)
        self.cgroup_root = str(cgroup_root)
        self.systemctl = systemctl or _systemctl

    def path(self, *parts) -> str:
        """One kernel entry: ("net/tcp",) or (pid, "fd")."""
        rel = "/".join(str(p) for p in parts)
        return f"/proc/{rel}" if self.root is None else str(self.root / rel)

    def listening_inodes(self, ports) -> set:
        """Inodes of the sockets listening on one of ``ports`` (the kernel's
        global socket tables only)."""
        ports = {int(p) for p in ports}
        found = set()
        for name in NET_TABLES:
            try:
                lines = Path(self.path(name)).read_text().splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                fields = line.split()
                if len(fields) < 10 or fields[3] != LISTEN:
                    continue
                try:
                    port = int(fields[1].rsplit(":", 1)[1], 16)
                except (IndexError, ValueError):
                    continue
                if port in ports:
                    found.add(fields[9])
        return found

    def policy_units(self) -> list:
        """Our ``levi-aeri-policy-*`` services and scopes that systemd knows
        (any state), as full unit names (``….service``/``….scope``). Raises
        ``OSError`` when systemd cannot be asked."""
        code, out = self.systemctl(
            [
                "systemctl",
                "--user",
                "list-units",
                "--all",
                "--plain",
                "--no-legend",
                "--type=service,scope",
                "levi-aeri-policy-*",
            ]
        )
        if code != 0:
            raise OSError(f"systemctl list-units: exit {code}")
        names = []
        for line in out.splitlines():
            first = line.split(None, 1)[0] if line.strip() else ""
            if POLICY_UNIT_FILE.fullmatch(first):
                names.append(first)
        return sorted(set(names))

    def unit_pids(self, unit: str) -> set:
        """The pids in our unit's ``cgroup.procs`` (empty: nothing runs in it).
        Raises ``OSError`` when it cannot be read: such a unit owns nothing."""
        if not unit.endswith((".service", ".scope")):
            unit += ".service"
        code, out = self.systemctl(
            [
                "systemctl",
                "--user",
                "show",
                "-p",
                "ControlGroup",
                "--value",
                unit,
            ]
        )
        if code != 0:
            raise OSError(f"systemctl show {unit}: exit {code}")
        group = out.strip()
        if not group:
            return set()
        if not group.startswith("/") or ".." in group.split("/"):
            raise OSError(f"unexpected control group of {unit}: {group!r}")
        # Read once: a process that joins or leaves the unit afterwards is
        # missed or kept for this one reading. Missing a new process keeps it
        # foreign (safe); a pid kept after it left is reused only after its
        # exit, when nvidia-smi no longer lists it for this reading's apps.
        text = Path(self.cgroup_root + group + "/cgroup.procs").read_text()
        try:
            return {int(x) for x in text.split()}
        except ValueError:
            raise OSError(f"malformed cgroup.procs of {unit}") from None

    def holds(self, pid: int, inodes) -> bool | None:
        """Does our ``pid`` hold one of the socket ``inodes``? None when its
        fds cannot be listed. Only ever called with a pid of our own units."""
        try:
            with os.scandir(self.path(pid, "fd")) as fds:
                for fd in fds:
                    try:
                        link = os.readlink(fd.path)
                    except OSError:
                        continue
                    if link.startswith("socket:[") and link[8:-1] in inodes:
                        return True
        except OSError:
            return None
        return False

    def descendants(self, pid: int, depth: int = 4) -> set:
        """``pid`` and its descendants, from ``<pid>/task/<tid>/children`` of
        our own pid and of the children found that way (all ours by descent;
        no other process is read). Raises ``OSError`` when a level cannot be
        read."""
        found, level = {int(pid)}, {int(pid)}
        for _ in range(depth):
            nxt = set()
            for p in level:
                with os.scandir(self.path(p, "task")) as tasks:
                    for task in tasks:
                        text = Path(task.path, "children").read_text()
                        nxt.update(int(x) for x in text.split())
            nxt -= found
            if not nxt:
                break
            found |= nxt
            level = nxt
        return found

    def servers(self, units, inodes) -> dict:
        """``{unit: pids}`` for each of our ``units`` one of whose processes
        holds a socket in ``inodes``: that unit serves the port. A unit whose
        cgroup cannot be read raises ``OSError`` (the caller exempts nothing);
        a unit whose listener's fds cannot be read does not serve."""
        out = {}
        if not inodes:
            return out
        for unit in units:
            pids = self.unit_pids(unit)
            if any(self.holds(p, inodes) is True for p in sorted(pids)):
                out[unit] = pids
        return out
