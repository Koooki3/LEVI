"""A stand-in for ``integrations/camera_preview/worker.py`` that opens no camera
and imports no ``pyrealsense2``: the tests of the preview start it as a real
process (in its own group, with a pipe on its standard input) to see the
lifecycle the manager promises: frames as files, SIGTERM and the end of the pipe
stop it, an exit code names a failure, and a process that ignores SIGTERM is
killed.

    fake_camera_worker.py --out DIR --mode MODE

Modes: ``ok`` (a picture of each camera ten times a second until told to stop),
``busy`` (exit 10), ``notfound`` (exit 11), ``silent`` (runs, writes nothing),
``stubborn`` (like ``ok``, and ignores SIGTERM), ``crash`` (one picture, then
exit 3).
"""

import argparse
import os
import signal
import sys
import threading
import time
from pathlib import Path

JPEG = (
    b"\xff\xd8\xff\xe0" + b"fake picture of {name} #{n}".ljust(64, b".") + b"\xff\xd9"
)


def write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--mode", default="ok")
    parser.add_argument("--camera", action="append", default=[])
    args, _ = parser.parse_known_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.mode == "busy":
        return 10
    if args.mode == "notfound":
        return 11
    stop = threading.Event()
    if args.mode != "stubborn":
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    else:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    def watch_stdin() -> None:
        # (the descriptor itself, as the real worker reads it: a thread blocked
        # in ``sys.stdin.buffer.read`` makes the interpreter abort at exit when a
        # signal stopped the process, so the exit code is lost)
        try:
            while os.read(0, 1024):
                pass
        except (OSError, ValueError):
            pass
        if args.mode != "stubborn":
            stop.set()

    threading.Thread(target=watch_stdin, daemon=True).start()
    n = 0
    while not stop.is_set():
        n += 1
        if args.mode != "silent":
            for name in ("side", "wrist"):
                data = JPEG.replace(b"{name}", name.encode()).replace(
                    b"{n}", str(n).encode()
                )
                write_atomic(out / f"{name}.jpg", data)
        if args.mode == "crash" and n >= 1:
            return 3
        time.sleep(0.1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
