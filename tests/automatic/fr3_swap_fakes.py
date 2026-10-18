"""Fakes for the FR3 policy switch tests: ``systemd-run``/``systemctl``,
a ``/proc`` tree, ``nvidia-smi`` and a monotonic clock. Nothing here runs a
unit tool, reads the real ``/proc`` or opens a socket."""

import shutil
import subprocess
from pathlib import Path


def refuse_unit_tools(monkeypatch):
    """No test starts systemctl, systemd-run or nvidia-smi for real."""
    real = subprocess.Popen.__init__

    def refuse(proc, args, *rest, **kwargs):
        argv = [args] if isinstance(args, str | bytes) else list(args)
        name = Path(str(argv[0])).name if argv else ""
        if name in ("systemctl", "systemd-run", "nvidia-smi"):
            raise AssertionError(f"test ran a real unit/GPU tool: {argv}")
        return real(proc, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", refuse)


def exec_start_value(argv):
    """What ``systemctl show -p ExecStart --value`` prints for a started unit."""
    if not argv:
        return ""
    return (
        f"{{ path={argv[0]} ; argv[]={' '.join(argv)} ; ignore_errors=no ; "
        "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
    )


HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
    "retrnsmt   uid  timeout inode\n"
)
TOTAL_MIB = 32607
VISION_MIB = 24100
POLICY_MIB = 7300


class Time:
    """A monotonic clock that moves on sleep (and by ``step`` per read)."""

    def __init__(self):
        self.t = 0.0
        self.step = 0.0
        self.tick = lambda: None

    def clock(self):
        self.t += self.step
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        self.tick()


class Proc:
    """A ``/proc`` tree: processes with a cgroup, a command line and the
    listening sockets of ``net/tcp``."""

    def __init__(self, root: Path):
        self.root = root
        # The unit cgroups (``/sys/fs/cgroup`` in production): ``cgroup.procs``
        # of each cgroup a process was spawned in.
        self.cgroup_root = root.parent / "cgroup"
        (root / "net").mkdir(parents=True)
        self.sockets = {}
        self.cgroups = {}  # pid -> cgroup path
        self.next_pid, self.next_inode = 2000, 70_000
        self.write()

    def write(self):
        rows = [
            f"   {n}: 0100007F:{port:04X} 00000000:0000 0A 00000000:00000000 "
            f"00:00000000 00000000  1000        0 {inode} 1 0000 100 0 0 10 0\n"
            for n, (inode, port) in enumerate(sorted(self.sockets.items()))
        ]
        (self.root / "net" / "tcp").write_text(HEADER + "".join(rows))
        (self.root / "net" / "tcp6").write_text(HEADER)

    def spawn(self, argv, cgroup, port=None):
        pid, self.next_pid = self.next_pid, self.next_pid + 1
        folder = self.root / str(pid)
        (folder / "fd").mkdir(parents=True)
        (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (folder / "cgroup").write_text(cgroup + "\n")
        self.cgroups[pid] = cgroup.split(":", 2)[-1]
        self._write_procs(self.cgroups[pid])
        if port is not None:
            self.listen(pid, port)
        return pid

    def _procs_file(self, group):
        return self.cgroup_root / group.lstrip("/") / "cgroup.procs"

    def _write_procs(self, group):
        members = sorted(p for p, g in self.cgroups.items() if g == group)
        f = self._procs_file(group)
        if members:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("".join(f"{p}\n" for p in members))
        elif f.exists():
            shutil.rmtree(f.parent)

    def control_group(self, unit):
        """``systemctl show -p ControlGroup``: empty once nothing runs in it."""
        group = f"/user.slice/user@1000.service/app.slice/{unit}.service"
        return group if self._procs_file(group).exists() else ""

    def listen(self, pid, port):
        inode, self.next_inode = self.next_inode, self.next_inode + 1
        (self.root / str(pid) / "fd" / "3").symlink_to(f"socket:[{inode}]")
        self.sockets[inode] = port
        self.write()

    def kill(self, pid):
        folder = self.root / str(pid)
        if not folder.exists():
            return
        for link in (folder / "fd").iterdir():
            target = str(link.readlink())
            if target.startswith("socket:["):
                self.sockets.pop(int(target[8:-1]), None)
        shutil.rmtree(folder)
        group = self.cgroups.pop(pid, None)
        if group is not None:
            self._write_procs(group)
        self.write()

    def alive(self):
        return sorted(int(p.name) for p in self.root.iterdir() if p.name.isdigit())


class Units:
    """``systemctl --user show/stop`` and ``systemd-run --user`` over ``Proc``;
    every call checks the hook (no motion, at most one policy listener)."""

    def __init__(self, proc, gpu, *, hook=lambda argv: None):
        self.proc, self.gpu, self.hook = proc, gpu, hook
        self.units = {}  # unit -> pid
        self.calls = []
        self.linger = 0  # polls a stopped process stays
        self.exit_early = 0  # next starts whose process dies before listening
        self.silent = False  # start, never listen
        self.argv_of = lambda argv: argv
        self.crash = None  # BaseException raised by systemd-run
        self.run_fails = 0  # next systemd-run calls that exit non-zero
        self.after_stop = lambda unit: None  # what happens right after a stop
        self._lingering = {}
        self.failed = set()
        self.log_files = {}  # unit -> the StandardOutput=append: path it was given
        self.banner = ""  # what a started service writes to that file
        self.exec_start = {}  # unit -> the argv systemd records as ExecStart

    def emit(self, unit, text):
        """What the service process writes to its stdout/stderr."""
        with open(self.log_files[unit], "a") as handle:
            handle.write(text)

    def run(self, argv):
        self.calls.append(list(argv))
        self.hook(argv)
        if argv[:2] == ["systemd-run", "--user"]:
            if self.crash is not None:
                raise self.crash
            if self.run_fails:
                self.run_fails -= 1
                return 1, ""
            unit = next(a for a in argv if a.startswith("--unit="))[7:]
            command = argv[argv.index("--") + 1 :]
            port = int(command[command.index("--port") + 1])
            cgroup = f"0::/user.slice/user@1000.service/app.slice/{unit}.service"
            for arg in argv:
                if arg.startswith("--property=StandardOutput=append:"):
                    self.log_files[unit] = arg.split("append:", 1)[1]
            if self.banner and unit in self.log_files:
                self.emit(unit, self.banner)
            if self.exit_early:
                self.exit_early -= 1
                self.failed.add(unit)
                return 0, ""
            listen = None if self.silent else port
            self.exec_start[unit] = self.argv_of(command)
            pid = self.proc.spawn(self.argv_of(command), cgroup, listen)
            self.units[unit] = pid
            self.gpu.add(pid)
            self.failed.discard(unit)
            return 0, f"Running as unit: {unit}.service\n"
        assert argv[:2] == ["systemctl", "--user"], argv
        unit = argv[-1].removesuffix(".service")
        if argv[2] == "show" and "ControlGroup" in argv:
            return 0, self.proc.control_group(unit) + "\n"
        if argv[2] == "show" and "ExecStart" in argv:
            return 0, exec_start_value(self.exec_start.get(unit)) + "\n"
        if argv[2] == "show":
            if unit in self.units:
                return 0, "active\n"
            return 0, ("failed\n" if unit in self.failed else "inactive\n")
        if argv[2] == "stop":
            pid = self.units.pop(unit, None)
            if pid is not None:
                if self.linger:
                    self._lingering[pid] = self.linger
                else:
                    self.proc.kill(pid)
                    self.gpu.exited(pid)
            self.after_stop(unit)
            return 0, ""
        raise AssertionError(argv)

    def tick(self):
        for pid in list(self._lingering):
            self._lingering[pid] -= 1
            if self._lingering[pid] <= 0:
                del self._lingering[pid]
                self.proc.kill(pid)
                self.gpu.exited(pid)
        self.gpu.tick()


class Smi:
    """``nvidia-smi`` answers: the vision model plus every policy process."""

    def __init__(self):
        self.pids = {4242: VISION_MIB}  # the live service's vLLM
        self.hold_after_exit = 0  # polls a dead policy pid stays listed
        self.other_mib = 0
        self._held = {}
        self.calls = []
        self.broken = False

    def add(self, pid):
        self.pids[pid] = POLICY_MIB

    def exited(self, pid):
        if self.hold_after_exit:
            self._held[pid] = self.hold_after_exit
        else:
            self.pids.pop(pid, None)

    def tick(self):
        for pid in list(self._held):
            self._held[pid] -= 1
            if self._held[pid] <= 0:
                del self._held[pid]
                self.pids.pop(pid, None)

    def run(self, argv):
        self.calls.append(list(argv))
        assert argv[0] == "nvidia-smi", argv
        if self.broken:
            return 9, ""
        if argv[1].startswith("--query-gpu"):
            used = sum(self.pids.values()) + self.other_mib
            return 0, f"{TOTAL_MIB}, {TOTAL_MIB - used}\n"
        return 0, "".join(f"{pid}, {mib}\n" for pid, mib in self.pids.items())


HOME = [0.4, 0.0, 0.2, 3.1, 0.0, 0.0]
