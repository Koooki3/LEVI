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
"""

import json
import os
import signal
import subprocess
import sys
import time

EXIT_NO_TTY = 97  # no controlling terminal could be acquired: nothing ran
EXIT_SPAWN = 98  # the client could not be started: nothing ran
KEYS = ("argv", "cwd", "exit_file")

_child: subprocess.Popen | None = None


def _say(text: str) -> None:
    """One line into the log (our stderr)."""
    print(f"hostchild: {text}", file=sys.stderr, flush=True)


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
    return spec


def main(argv) -> int:
    global _child
    if len(argv) != 2:
        _say("usage: hostchild.py <spec.json>")
        return EXIT_SPAWN
    try:
        spec = _load(argv[1])
    except (OSError, ValueError) as exc:
        _say(f"the spec could not be read ({exc.__class__.__name__})")
        return EXIT_SPAWN
    for name in ("SIGINT", "SIGTERM", "SIGTSTP", "SIGQUIT"):
        signal.signal(getattr(signal, name), _ignore)
    signal.signal(signal.SIGHUP, _hangup)
    if not _controlling_tty():
        _say("no controlling terminal; the client was not started")
        _write_exit(spec["exit_file"], None, EXIT_NO_TTY, None)
        return EXIT_NO_TTY
    try:
        _child = subprocess.Popen(
            spec["argv"], cwd=spec["cwd"], close_fds=True, stdin=None
        )
    except OSError as exc:
        _say(f"the client could not be started ({exc.__class__.__name__})")
        _write_exit(spec["exit_file"], None, EXIT_SPAWN, None)
        return EXIT_SPAWN
    _say(f"client started, pid {_child.pid}")
    code = _child.wait()
    sig = -code if code < 0 else None
    _write_exit(spec["exit_file"], _child.pid, None if sig else code, sig)
    _say(f"client ended ({'signal ' + str(sig) if sig else 'code ' + str(code)})")
    return 128 + sig if sig else code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
