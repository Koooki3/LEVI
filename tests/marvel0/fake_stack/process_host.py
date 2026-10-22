"""L2 of the stack actions: ``systemd-run`` and ``systemctl`` mapped onto real
child processes of this test run (``subprocess.Popen`` in a session of their
own), so the stand-in programs of this folder really print, really listen and
really stop on SIGINT. No systemd is involved: ``KillSignal``,
``TimeoutStopSec`` and ``SendSIGKILL`` are honoured from the properties the
page passed, which is what makes the rehearsal check them.

Nothing but a stand-in program is ever run (``fake_specs.assert_fake``), and
only SIGINT (the unit's ``KillSignal``) is ever sent to a unit; ``reap``
kills whatever a test left behind, outside the page's own calls."""

import os
import signal
import subprocess
import threading
import time

from . import fake_specs

KILL_SIGNALS = {"SIGINT": signal.SIGINT, "SIGTERM": signal.SIGTERM}


class Proc:
    def __init__(self, popen, name, argv, props):
        self.popen = popen
        self.name = name
        self.argv = argv
        self.props = props
        self.stopped = False  # the page stopped it
        self.timed_out = False
        self.signals = []  # what the page's stops sent

    def alive(self) -> bool:
        return self.popen.poll() is None


class ProcessHost:
    def __init__(self, wrapper=None):
        self.units = {}  # name -> Proc
        self._units_lock = threading.Lock()
        self.wrapper = wrapper
        self.commands = []  # every argv the page asked to run
        self.oneshot_timeouts = []  # one-shots RuntimeMaxSec had to end

    def _collect(self, name, proc):
        """A status read and a stop can observe the same clean exit.
        Collect that instance once, never a later unit with the same name."""
        with self._units_lock:
            if proc is not None and self.units.get(name) is proc:
                self.units.pop(name)

    # -- the runner the page uses (behind ``stack.guarded``)
    def run(self, argv, timeout_s):
        self.commands.append(list(argv))
        if argv[0] == "systemd-run":
            return self._systemd_run(argv, timeout_s)
        if argv[0] == "loginctl":
            return 0, "Linger=no\n"
        verb = argv[2]
        if verb == "show-environment":
            return 0, "PATH=/usr/bin\nHOME=/home/x\nROS_DISTRO=jazzy\n"
        if verb == "show" and len(argv) == 4:
            return 0, "DefaultLimitRTPRIO=99\nDefaultLimitMEMLOCK=infinity\n"
        if verb == "show":
            return 0, self._show(argv[3])
        if verb == "stop":
            return self._stop(argv[3], timeout_s)
        if verb == "kill":
            return self._kill(argv)
        if verb == "reset-failed":
            name = argv[3].removesuffix(".service")
            proc = self.units.get(name)
            if proc is not None and not proc.alive():
                self._collect(name, proc)
            return 0, ""
        raise AssertionError(f"unexpected command {argv}")

    @staticmethod
    def _properties(options):
        props = {}
        for item in options:
            if item.startswith("--property="):
                key, _, value = item[len("--property=") :].partition("=")
                props[key] = value
        return props

    def _systemd_run(self, argv, timeout_s):
        cut = argv.index("--")
        options, command = argv[:cut], argv[cut + 1 :]
        fake_specs.assert_fake(command, wrapper=self.wrapper)
        props = self._properties(options)
        name = next(o.split("=", 1)[1] for o in options if o.startswith("--unit="))
        env = dict(os.environ)
        for item in options:
            if item.startswith("--setenv="):
                key, _, value = item[len("--setenv=") :].partition("=")
                env[key] = value
        if "--pipe" in options:
            return self._oneshot(command, props, env, timeout_s)
        with self._units_lock:
            if name in self.units and self.units[name].alive():
                return (
                    1,
                    f"Unit {name}.service was already loaded or has a fragment file.",
                )
            out = props["StandardOutput"].split("append:", 1)[1]
            with open(out, "ab", buffering=0) as log:
                popen = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=env,
                )
            self.units[name] = Proc(popen, name, command, props)
        return 0, ""

    def _oneshot(self, command, props, env, timeout_s):
        """``systemd-run --pipe --wait``: the command's output comes back, and
        ``RuntimeMaxSec`` ends it with the unit's ``KillSignal`` (the page's
        backstop behind `timeout`). What status the real ``systemd-run`` exits
        with after that is not known (it is to be seen in the rehearsal): the
        stand-in says 1, and nothing in the page may depend on it. The output
        printed until then is returned, standard error included, as the real
        client would print it."""
        limit = min(float(props.get("RuntimeMaxSec", timeout_s)), timeout_s)
        popen = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        try:
            out, err = popen.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            self.oneshot_timeouts.append(command)
            signame = props.get("KillSignal", "SIGTERM")
            os.killpg(popen.pid, KILL_SIGNALS[signame])
            try:
                out, err = popen.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                # (the real unit gets SIGKILL after TimeoutStopSec; this is
                # test hygiene, not the page)
                os.killpg(popen.pid, signal.SIGKILL)
                out, err = popen.communicate()
            return 1, (out or "") + (err or "")
        return popen.returncode, out + (err if popen.returncode else "")

    def _show(self, unit):
        name = unit.removesuffix(".service")
        proc = self.units.get(name)
        if proc is None or (
            not proc.alive() and proc.popen.returncode == 0 and not proc.timed_out
        ):
            # (a unit that exited cleanly is collected, as systemd does)
            self._collect(name, proc)
            return (
                f"Id={unit}\nLoadState=not-found\nActiveState=inactive\nSubState=dead\n"
            )
        if proc.timed_out:
            active, sub, result = "failed", "failed", "timeout"
        elif proc.alive():
            active, sub, result = "active", "running", "success"
        else:
            active, sub, result = "failed", "failed", "exit-code"
        return (
            f"Id={unit}\nLoadState=loaded\nActiveState={active}\nSubState={sub}\n"
            f"Result={result}\n"
            f"ExecStart={{ path={proc.argv[0]} ; argv[]={' '.join(proc.argv)} ; ignore_errors=no }}\n"
            f"MainPID={proc.popen.pid if proc.alive() else 0}\nNRestarts=0\n"
            f"TasksCurrent={1 if proc.alive() else 0}\n"
        )

    def _kill(self, argv):
        """``systemctl --user kill --signal=SIGINT <unit>``: the unit's own
        stop signal once more, to its control group. Nothing else is sent."""
        assert argv[3] == "--signal=SIGINT", argv
        proc = self.units.get(argv[4].removesuffix(".service"))
        if proc is not None and proc.alive():
            proc.signals.append("SIGINT")
            os.killpg(proc.popen.pid, signal.SIGINT)
        return 0, ""

    def _stop(self, unit, timeout_s):
        name = unit.removesuffix(".service")
        proc = self.units.get(name)
        if proc is None:
            return 0, ""
        if not proc.alive():
            self._collect(name, proc)
            return 0, ""
        signame = proc.props.get("KillSignal", "SIGTERM")
        wait_s = float(proc.props.get("TimeoutStopSec", "5"))
        proc.stopped = True
        proc.signals.append(signame)
        os.killpg(proc.popen.pid, KILL_SIGNALS[signame])
        deadline = time.monotonic() + wait_s
        while proc.alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        if proc.alive():
            # SendSIGKILL=no: the page never kills; the unit is failed.
            assert proc.props.get("SendSIGKILL") == "no"
            proc.timed_out = True
            return 1, "stop timed out"
        self._collect(name, proc)
        return 0, ""

    # -- test hygiene (never the page's)
    def reap(self):
        with self._units_lock:
            units = list(self.units.items())
        for name, proc in units:
            if proc.alive():
                try:
                    os.killpg(proc.popen.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.popen.wait(timeout=5)
            self._collect(name, proc)
