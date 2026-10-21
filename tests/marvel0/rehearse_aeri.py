"""L3 of the hosted AERI terminal D: the launcher and the stand-in terminal D in a
real ``systemd-run --user --scope``, under a real pty. Run it once, by hand, with
the arm off (**it refuses to run, exit 2, when a robot process is on the
machine**: ``ros2_control_node``, ``franka_server``, ``enpire_franka``,
``serve_policy`` or ``run_robotiq_client``):

    uv run --no-sync python tests/marvel0/rehearse_aeri.py [--work DIR]

It proves what no fake machine can, and measures what the design only assumed
(its "unverified" list): that a scope started the way the page starts it accepts
the user bus environment and runs the launcher and the stand-in; that the Ctrl+C
byte written to the pty arrives as **one** SIGINT at the stand-in, also behind the
**real** ``uv run --no-project --no-sync python`` (does ``uv`` forward it a second
time?); that a stand-in behind a wrapper which does forward (a second SIGINT) is
the double signal the page must not cause; what the kernel does when the
pty's master closes (the stand-in must see SIGINT, and no SIGHUP before it); the
timing SIGINT, grace, SIGKILL; that a scope **survives** the process that started
it for the stand-in's wrap-up (what a restart of the product does to its
children); and that nothing is left in ``levi-aeri-*rehearsal*`` at the end.

**Nothing real runs.** The units are named with ``rehearsal``; the commands are
``fake_aeri.py`` (and ``uv`` only to start the stand-in: no ``--with``, no openpi
project, the real AERI is never started); the folders are temporary; no port is
opened. Leftovers fail the run."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FAKE = HERE / "fake_aeri.py"
HOSTCHILD = ROOT / "levi" / "marvel0" / "hostchild.py"
TAG = "levi-aeri-termd-marvel0-rehearsal-"
ROBOT_PATTERN = (
    "ros2_control_node|franka_server|enpire_franka|serve_policy|run_robotiq_client"
)
RESULTS: list = []
CORE = r"""
import os, subprocess, sys, time
unit, hostchild, spec, env_pairs = sys.argv[1:5]
master, slave = os.openpty()
env = dict(p.split("=", 1) for p in env_pairs.split("\x1f") if p)
bus = {k: os.environ[k] for k in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS") if k in os.environ}
log = open(spec + ".launcher.log", "w")
argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit=" + unit,
        "--description=AERI terminal D rehearsal", "--", "/usr/bin/env", "-i",
        *[f"{k}={v}" for k, v in env.items()], sys.executable, "-I", hostchild, spec]
child = subprocess.Popen(argv, stdin=slave, stdout=log, stderr=log, env=bus,
                         start_new_session=True, close_fds=True)
os.close(slave)
print("pid", child.pid, flush=True)
for line in sys.stdin:
    word, _, rest = line.rstrip("\n").partition(" ")
    if word == "write":
        os.write(master, bytes.fromhex(rest))
    elif word == "close":
        os.close(master)
        master = None
        print("closed", flush=True)
    elif word == "exit":
        break
"""


def say(text=""):
    print(text, flush=True)


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    say(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    return ok


def systemctl(*args, timeout=15):
    done = subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return done.returncode, done.stdout + done.stderr


def units():
    _, text = systemctl(
        "list-units", "--all", "--no-legend", "--plain", "--full", f"{TAG}*"
    )
    return [line.split()[0] for line in text.splitlines() if line.strip()]


def robots() -> bool:
    out = subprocess.run(
        ["ps", "-eo", "args"], capture_output=True, text=True, check=False
    ).stdout
    return any(re.search(ROBOT_PATTERN, line) for line in out.splitlines())


def wait_for(predicate, timeout=20.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class Run:
    """One launcher in one scope, with a core process that holds the master."""

    def __init__(self, work: Path, n: int, argv_prefix, **knobs):
        self.dir = work / f"run{n}"
        self.dir.mkdir()
        self.name = f"aeri-20261021-ext-r{n:02d}"
        folder = self.dir / "runs" / self.name
        folder.mkdir(parents=True)
        (folder / "job.yaml").write_text(
            f"experiment:\n  name: {self.name}\n  episodes: 2\nsubtask:\n  max_steps: 4000\n"
        )
        (folder / "binding.json").write_text("{}")
        self.rollout = self.dir / "rollout"
        (self.rollout / ".aeri" / "runs").mkdir(parents=True)
        self.signals = self.dir / "signals.log"
        (folder / ".fake_aeri.json").write_text(
            json.dumps(
                {
                    "rollout_root": str(self.rollout),
                    "service_s": 0.05,
                    "step_s": 0.05,
                    "shutdown_s": 0.3,
                    "signal_log": str(self.signals),
                    **knobs,
                }
            )
        )
        self.out = self.dir / "screen.log"
        self.events = self.dir / "events"
        self.exit = self.dir / "exit.json"
        self.spec = self.dir / "spec.json"
        self.unit = f"{TAG}{n:02d}"
        argv = [
            *argv_prefix,
            str(FAKE),
            "execute",
            "--config",
            str(folder / "job.yaml"),
            "--binding",
            str(folder / "binding.json"),
            "--expect-plan",
            "a" * 64,
            "--expect-binding",
            "b" * 64,
        ]
        self.spec.write_text(
            json.dumps(
                {
                    "mode": "aeri",
                    "argv": argv,
                    "cwd": str(self.dir),
                    "exit_file": str(self.exit),
                    "out_log": str(self.out),
                    "events_file": str(self.events),
                    "hangup_grace_s": 1.0,
                }
            )
        )
        home = os.environ["HOME"]
        env = {
            "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": home,
            "USER": os.environ.get("USER", ""),
            "LANG": "C.UTF-8",
            "TERM": "dumb",
            "PYTHONUNBUFFERED": "1",
            "UV_OFFLINE": "1",
            "LEVI_AERI_CONSOLE_LANG": "zh",
        }
        for name in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
            if name in os.environ:
                env[name] = os.environ[name]
        self.core = subprocess.Popen(
            [
                sys.executable,
                "-c",
                CORE,
                self.unit,
                str(HOSTCHILD),
                str(self.spec),
                "\x1f".join(f"{k}={v}" for k, v in env.items()),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.core.stdout.readline()

    def text(self) -> str:
        return self.out.read_text(errors="replace") if self.out.exists() else ""

    def write(self, data: bytes):
        self.core.stdin.write("write " + data.hex() + "\n")
        self.core.stdin.flush()

    def close_master(self):
        self.core.stdin.write("close\n")
        self.core.stdin.flush()

    def to_forward(self) -> bool:
        if not wait_for(lambda: "请输入" in self.text(), 30):
            return False
        self.write(f"execute {self.name}\r".encode())
        if not wait_for(lambda: "清单对就按 y" in self.text(), 60):
            return False
        time.sleep(0.5)
        self.write(b"y")
        if not wait_for(lambda: "确认场景就绪" in self.text(), 30):
            return False
        time.sleep(0.5)
        self.write(b"\r")
        return wait_for(lambda: "子任务进行中" in self.text(), 30)

    def signal_count(self) -> int:
        return (
            len(self.signals.read_text().split("\n")) - 1
            if self.signals.exists()
            else 0
        )

    def gone(self, timeout=20.0) -> bool:
        return wait_for(
            lambda: not [u for u in units() if u.startswith(self.unit)], timeout
        )

    def cleanup(self):
        try:
            self.core.stdin.write("exit\n")
            self.core.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            self.core.wait(5)
        except subprocess.TimeoutExpired:
            self.core.kill()
        systemctl("stop", f"{self.unit}.scope", timeout=20)


WRAPPER = r"""
import os, signal, subprocess, sys
child = subprocess.Popen(sys.argv[1:])
signal.signal(signal.SIGINT, lambda s, f: child.send_signal(signal.SIGINT))  # forwards a second one
sys.exit(child.wait())
"""


def scenarios(work: Path):
    say("scope, phrase, services, one Ctrl+C (direct)")
    run = Run(work, 1, [sys.executable])
    try:
        ok = run.to_forward()
        check(
            "the scope started the launcher and the stand-in reached the forward loop",
            ok,
        )
        _, text = systemctl(
            "show", f"{run.unit}.scope", "--property=ActiveState,ControlGroup"
        )
        check(
            "the scope is active", "ActiveState=active" in text, text.replace("\n", " ")
        )
        cgroup = re.search(r"ControlGroup=(.*)", text)
        procs = (
            Path("/sys/fs/cgroup" + cgroup.group(1) + "/cgroup.procs")
            if cgroup
            else None
        )
        count = (
            len(procs.read_text().split())
            if procs is not None and procs.exists()
            else 0
        )
        check(
            "the scope's cgroup holds the launcher and the stand-in",
            count >= 2,
            f"{count} processes",
        )
        before = run.signal_count()
        run.write(b"\x03")
        check(
            "the stand-in ends on the pty's Ctrl+C",
            wait_for(lambda: run.exit.exists(), 40),
        )
        check(
            "exactly one SIGINT reached the stand-in",
            run.signal_count() - before == 1,
            f"{run.signal_count()}",
        )
        record = json.loads(run.exit.read_text()) if run.exit.exists() else {}
        check("the exit was recorded as 130", record.get("code") == 130, str(record))
        check("the scope is gone afterwards", run.gone())
    finally:
        run.cleanup()

    say("one Ctrl+C behind the real uv")
    uv = shutil.which("uv")
    run = (
        Run(work, 2, [uv, "run", "--no-project", "--no-sync", "python"]) if uv else None
    )
    if run is None:
        check("uv is installed (the real one is part of the rehearsal)", False)
    else:
        try:
            ok = run.to_forward()
            check("the stand-in started behind the real uv", ok, run.text()[-200:])
            if ok:
                before = run.signal_count()
                run.write(b"\x03")
                check("it ends", wait_for(lambda: run.exit.exists(), 40))
                n = run.signal_count() - before
                check(
                    "the real uv forwards no second SIGINT (the stand-in saw exactly one)",
                    n == 1,
                    f"{n} signal(s)",
                )
        finally:
            run.cleanup()

    say("one Ctrl+C behind a wrapper that forwards it (the double signal)")
    wrapper = work / "wrapper.py"
    wrapper.write_text(WRAPPER)
    run = Run(work, 3, [sys.executable, str(wrapper), sys.executable])
    try:
        ok = run.to_forward()
        if ok:
            before = run.signal_count()
            run.write(b"\x03")
            wait_for(lambda: run.exit.exists(), 40)
            n = run.signal_count() - before
            check(
                "a forwarding parent makes it two (this is what the page must not rely on)",
                n == 2,
                f"{n} signal(s)",
            )
        else:
            check("the wrapper case started", False)
    finally:
        run.cleanup()

    say("the master closes: SIGINT first, the scope survives the wrap-up")
    run = Run(work, 4, [sys.executable])
    try:
        ok = run.to_forward()
        check("forward loop reached", ok)
        t0 = time.monotonic()
        run.close_master()
        check(
            "the stand-in ends on the hang-up",
            wait_for(lambda: run.exit.exists(), 40),
            f"{time.monotonic() - t0:.1f} s",
        )
        text = run.text()
        check("it saw the interrupt (SIGINT first)", "Ctrl+C" in text and "7/7" in text)
        check(
            "it was not killed: the exit was recorded as 130",
            json.loads(run.exit.read_text()).get("code") == 130
            if run.exit.exists()
            else False,
        )
        check("the scope is gone afterwards", run.gone())
    finally:
        run.cleanup()

    say("the master closes and D ignores SIGINT: SIGKILL after the grace")
    run = Run(work, 5, [sys.executable], ignore_sigint=True)
    try:
        ok = run.to_forward()
        check("forward loop reached", ok)
        arm = run.rollout / "fake_arm.jsonl"
        t0 = time.monotonic()
        run.close_master()
        check(
            "the scope is gone (the group was killed)",
            run.gone(30),
            f"{time.monotonic() - t0:.1f} s",
        )
        n1 = len(arm.read_text().split("\n")) if arm.exists() else 0
        time.sleep(0.5)
        n2 = len(arm.read_text().split("\n")) if arm.exists() else 0
        check("no motion after the kill", n1 == n2, f"{n1} lines")
        check("no exit record (a killed launcher writes none)", not run.exit.exists())
    finally:
        run.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", type=Path)
    args = parser.parse_args()
    if robots():
        say("a robot process is on the machine; the rehearsal does not run (exit 2)")
        return 2
    work = args.work or Path(tempfile.mkdtemp(prefix="rehearse-aeri-"))
    work.mkdir(parents=True, exist_ok=True)
    say(f"work folder {work}")
    try:
        scenarios(work)
    finally:
        left = units()
        check("no levi-aeri-*rehearsal* unit is left", not left, ", ".join(left))
        if args.work is None:
            shutil.rmtree(work, ignore_errors=True)
    failed = [name for name, ok, _ in RESULTS if not ok]
    say(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} checks passed")
    for name in failed:
        say(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
