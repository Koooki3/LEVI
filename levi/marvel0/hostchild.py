"""The launcher of the hosted evaluation client (``levi/marvel0/run.py``).

Run by the LEVI core as ``python -I hostchild.py <spec.json>`` inside a
``systemd-run --user --scope`` of its own, in a **new session**, with the pty
slave as stdin and a private log file as stdout and stderr. Standard library
only; it imports nothing of LEVI and is started with ``-I`` (no current folder,
no environment path) so that nothing but this file decides what it does.

What it does, in order:

1. Makes the pty its **controlling terminal** (``TIOCSCTTY``). A hang-up of
   that terminal (the core's master fd closing, because the core ended, however
   it ended) then reaches this session leader as SIGHUP + SIGCONT. Without a
   controlling terminal there is no such chain, so it gives up instead
   (exit 97): a client that outlives its keyboard keeps moving the arm.
2. Starts the client from ``spec["argv"]`` in ``spec["cwd"]`` **in its own
   process group** (the one the terminal's foreground group is): a Ctrl+C byte
   or a ``killpg`` meant for the client reaches this launcher too, which is why
   it survives SIGINT, SIGTERM and SIGTSTP with handlers that do nothing and
   leaves SIGSTOP and SIGKILL alone (they cannot be handled anyway).
3. On **SIGHUP** it kills every process group of the client, and its own, with
   SIGKILL. Nothing may keep stepping without a keyboard, and ``uv`` may put
   the client in a group of its own, which the kernel's own hang-up of the
   foreground group would miss.
4. When the client ends it writes its exit to ``spec["exit_file"]`` (0600,
   written whole) and exits with the same code (128 + n for a signal).

The core never sends a signal to this process other than SIGSTOP (hold) and
SIGKILL (end).

**AERI mode** (``spec["mode"] == "aeri"``, terminal D of the autonomy system,
``levi/marvel0/aeri_run.py``): everything above holds except three things.
The client's fd 1 and 2 are the slave of an **output pty of this launcher's
own** (window 50 x 200), never the log file: ``isatty`` is true there (the
console refuses to run otherwise) while fd 0 stays the controlling terminal
with the keys and the termios modes. A thread of this launcher drains that pty
into ``spec["out_log"]`` (``0600``, appended, never followed through a link) so
that **the core stopping, hanging or dying never blocks the client's output**
(a full pty would stall its heartbeat thread, and with it Ctrl+C and the
safe shutdown); a write error (a full disk) drops the bytes and goes on. And a
hang-up (SIGHUP) sends **SIGINT** to the client's groups and to ours and only
``hangup_grace_s`` later (SIGALRM) SIGKILL: terminal D wraps up on SIGINT
(stops the motion, holds the arm) and the grace is the user's decision of
2026-10-21 (3 s). ``spec["events_file"]`` (optional) gets one
``[HH:MM:SS] terminal D exited rc=<n>`` line when the client ends, the line
``wait_request.sh`` and ``res_monitor.py`` of the AERI tools wait for. A spec
without ``mode`` behaves exactly as before.
"""

import errno
import json
import os
import signal
import struct
import subprocess
import sys
import threading
import time

EXIT_NO_TTY = 97  # no controlling terminal could be acquired: nothing ran
EXIT_SPAWN = 98  # the client could not be started: nothing ran
KEYS = ("argv", "cwd", "exit_file")
AERI = "aeri"
AERI_WINDOW = (50, 200)  # rows, columns of the client's output pty
AERI_GRACE_S = 3.0  # SIGINT, then SIGKILL this much later (user, 2026-10-21)
AERI_DRAIN_JOIN_S = 5.0  # how long to wait for the last output after the client ends

_child: subprocess.Popen | None = None
_grace_s = AERI_GRACE_S


def _say(text: str) -> None:
    """One line into the log (our stderr). A log that cannot be written (the
    terminal is gone and our stderr was it) never raises: this is also called
    from signal handlers, where an exception would escape into whatever the
    main thread was doing."""
    try:
        print(f"hostchild: {text}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _write_exit(path: str, pid, code, sig) -> None:
    """The exit record, whole or not at all, readable by the owner only."""
    tmp = f"{path}.{os.getpid()}.tmp"
    data = json.dumps({"pid": pid, "code": code, "signal": sig, "at": time.time()})
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(data + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        _say(f"cannot write the exit record ({exc.__class__.__name__})")


def _ignore(signum, frame) -> None:
    """SIGINT, SIGTERM, SIGTSTP: meant for the client; the launcher stays."""


def _hangup(signum, frame) -> None:
    """The terminal is gone: nothing may run on. The client's group first (it
    may differ from ours), then our own, which holds this process too."""
    own = os.getpgrp()
    if _child is not None:
        try:
            other = os.getpgid(_child.pid)
        except OSError:
            other = own
        if other != own:
            try:
                os.killpg(other, signal.SIGKILL)
            except OSError:
                pass
    os.killpg(own, signal.SIGKILL)


def _groups() -> list:
    """The process groups to signal on a hang-up: the client's (it may differ
    from ours: ``uv`` may put it in a group of its own) first, then our own."""
    own = os.getpgrp()
    groups = []
    if _child is not None:
        try:
            other = os.getpgid(_child.pid)
        except OSError:
            other = own
        if other != own:
            groups.append(other)
    groups.append(own)
    return groups


def _hangup_aeri(signum, frame) -> None:
    """AERI mode: the terminal is gone. SIGINT first (terminal D stops the
    motion, holds the arm and shuts its services down on it), SIGKILL after the
    grace (``_alarm``). SIGINT reaches this process too (its group); it
    ignores it."""
    for group in _groups():
        try:
            os.killpg(group, signal.SIGINT)
        except OSError:
            pass
    try:
        signal.setitimer(signal.ITIMER_REAL, _grace_s)
    except (OSError, ValueError):
        # No timer, no grace: the old behaviour, a kill now.
        _alarm(signal.SIGALRM, None)
    _say("hang-up: SIGINT to the client, SIGKILL after the grace")


def _alarm(signum, frame) -> None:
    """The grace is over: nothing may run on without a keyboard."""
    for group in _groups():
        try:
            os.killpg(group, signal.SIGKILL)
        except OSError:
            pass


def _controlling_tty() -> bool:
    """Make fd 0 (the pty slave) the controlling terminal of this session, and
    make sure this process group is its foreground group."""
    if not os.isatty(0):
        return False
    import fcntl
    import termios

    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        # Already ours is fine; anything else is not.
        try:
            if os.tcgetpgrp(0) != os.getpgrp():
                return False
        except OSError:
            return False
    try:
        return os.tcgetpgrp(0) == os.getpgrp()
    except OSError:
        return False


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        spec = json.load(handle)
    if not isinstance(spec, dict) or any(
        not isinstance(spec.get(k), (list, str)) for k in KEYS
    ):
        raise ValueError("the spec is not what the core writes")
    if not spec["argv"] or not all(isinstance(a, str) for a in spec["argv"]):
        raise ValueError("argv must be a list of strings")
    mode = spec.get("mode")
    if mode is not None and mode != AERI:
        raise ValueError("unknown mode")
    if mode == AERI:
        if not isinstance(spec.get("out_log"), str) or not spec["out_log"]:
            raise ValueError("an aeri spec needs out_log")
        events = spec.get("events_file")
        if events is not None and not isinstance(events, str):
            raise ValueError("events_file must be a string")
        grace = spec.get("hangup_grace_s", AERI_GRACE_S)
        if (
            isinstance(grace, bool)
            or not isinstance(grace, (int, float))
            or not 0 < grace <= 30
        ):
            raise ValueError("hangup_grace_s must be a number in (0, 30]")
    return spec


class _Output:
    """AERI mode: the client's output pty and the thread that drains it into
    ``out_log``. Nothing waits for anyone: a write error drops the bytes."""

    def __init__(self, out_log: str):
        import fcntl
        import termios

        self.master, self.slave = os.openpty()
        rows, cols = AERI_WINDOW
        fcntl.ioctl(
            self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
        )
        self.fd = os.open(
            out_log,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        self.dropped = 0
        self.stop = False
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="aeri-out", daemon=True)
        self.thread.start()

    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def _write(self, data: bytes) -> None:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(self.fd, view) :]
        except OSError:
            self.dropped += len(data)  # a full disk never stops the client

    def _run(self) -> None:
        """Read until the client's side is gone (EIO) or this launcher asks to
        stop. Every other error is survived: the client must never block on
        its screen."""
        while True:
            try:
                data = os.read(self.master, 1 << 16)
            except InterruptedError:
                continue
            except OSError as exc:
                if exc.errno == errno.EIO:
                    return  # every slave end is closed: the client is gone
                time.sleep(0.05)
                if self.stop:
                    return
                continue
            if not data:
                return
            self._write(data)

    def finish(self) -> None:
        """After the client ended: the last of its output, then close."""
        self.stop = True
        deadline = time.monotonic() + AERI_DRAIN_JOIN_S
        while self.alive() and time.monotonic() < deadline:
            self.thread.join(0.1)
        for fd in (self.master, self.fd):
            try:
                os.close(fd)
            except OSError:
                pass


def _event(path, text: str) -> None:
    """One line of the run's event file (what the AERI tools wait for)."""
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        with os.fdopen(fd, "a") as handle:
            handle.write(f"[{time.strftime('%H:%M:%S')}] {text}\n")
    except OSError:
        pass


def main(argv) -> int:
    global _child, _grace_s
    if len(argv) != 2:
        _say("usage: hostchild.py <spec.json>")
        return EXIT_SPAWN
    try:
        spec = _load(argv[1])
    except (OSError, ValueError) as exc:
        _say(f"the spec could not be read ({exc.__class__.__name__})")
        return EXIT_SPAWN
    aeri = spec.get("mode") == AERI
    for name in ("SIGINT", "SIGTERM", "SIGTSTP", "SIGQUIT"):
        signal.signal(getattr(signal, name), _ignore)
    if aeri:
        # The grace timer's handler first: a hang-up that arrives at once must
        # find it installed.
        _grace_s = float(spec.get("hangup_grace_s", AERI_GRACE_S))
        signal.signal(signal.SIGALRM, _alarm)
        signal.signal(signal.SIGHUP, _hangup_aeri)
    else:
        signal.signal(signal.SIGHUP, _hangup)
    if not _controlling_tty():
        _say("no controlling terminal; the client was not started")
        _write_exit(spec["exit_file"], None, EXIT_NO_TTY, None)
        return EXIT_NO_TTY
    output = None
    if aeri:
        try:
            output = _Output(spec["out_log"])
        except OSError as exc:
            _say(f"the output could not be set up ({exc.__class__.__name__})")
            _write_exit(spec["exit_file"], None, EXIT_SPAWN, None)
            return EXIT_SPAWN
    try:
        if output is None:
            _child = subprocess.Popen(
                spec["argv"], cwd=spec["cwd"], close_fds=True, stdin=None
            )
        else:
            _child = subprocess.Popen(
                spec["argv"],
                cwd=spec["cwd"],
                close_fds=True,
                stdin=None,
                stdout=output.slave,
                stderr=output.slave,
            )
    except OSError as exc:
        _say(f"the client could not be started ({exc.__class__.__name__})")
        _write_exit(spec["exit_file"], None, EXIT_SPAWN, None)
        if output is not None:
            os.close(output.slave)
            output.finish()
        return EXIT_SPAWN
    if output is not None:
        os.close(output.slave)  # our copy: the client's side is the only one left
        output.start()
    _say(f"client started, pid {_child.pid}")
    if output is None:
        code = _child.wait()
    else:
        code = _wait_aeri(_child, output)
    sig = -code if code < 0 else None
    if aeri:
        with _timer_off():
            _write_exit(spec["exit_file"], _child.pid, None if sig else code, sig)
        output.finish()
        if spec.get("events_file"):
            rc = 128 + sig if sig else code
            _event(spec["events_file"], f"terminal D exited rc={rc}")
    else:
        _write_exit(spec["exit_file"], _child.pid, None if sig else code, sig)
    _say(f"client ended ({'signal ' + str(sig) if sig else 'code ' + str(code)})")
    return 128 + sig if sig else code


class _timer_off:
    """The grace timer is cancelled while the exit is recorded: the client is
    gone, there is nothing left to kill but this launcher."""

    def __enter__(self):
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
        except (OSError, ValueError):
            pass

    def __exit__(self, *exc):
        return False


def _wait_aeri(child, output) -> int:
    """Wait for the client; meanwhile keep the drain thread alive (a thread
    that died is started again: the client must never block on its screen)."""
    while True:
        try:
            return child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if not output.alive():
                output.start()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
