"""Single uv entry point supervising frontend and backend."""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from .paths import PROJECT, configure


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "clean":
        from .maintenance import main as clean

        sys.argv.pop(1)
        return clean()
    if len(sys.argv) > 1 and sys.argv[1] == "convert":
        from .conversion.__main__ import main as convert

        sys.argv.pop(1)
        return convert()
    parser = argparse.ArgumentParser(description="LEVI · LeRobot dataset workbench")
    parser.add_argument(
        "command",
        choices=["setup", "dev", "serve", "build", "backend", "check"],
        nargs="?",
        default="serve",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Frontend bind address; use 0.0.0.0 only behind trusted access control",
    )
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--backend-port", type=int, default=7861)
    args = parser.parse_args()
    configure()
    from .bootstrap import setup, bun_path

    if args.command == "setup":
        setup()
        return
    if args.command == "backend":
        import uvicorn

        uvicorn.run("levi.service:app", host="127.0.0.1", port=args.backend_port)
        return
    bun = str(bun_path()) if bun_path().exists() else shutil.which("bun")
    if not bun:
        parser.error("Run uv run levi setup first")
    os.environ["PATH"] = (
        str(PROJECT / ".venv/bin")
        + os.pathsep
        + str(Path(bun).parent)
        + os.pathsep
        + os.environ["PATH"]
    )
    os.environ["LEVI_BACKEND_URL"] = f"http://127.0.0.1:{args.backend_port}"
    os.environ["NEXT_TELEMETRY_DISABLED"] = "1"
    if not Path(bun).exists():
        parser.error("Bun is required: see README installation instructions")
    if args.command in ("build", "check"):
        raise SystemExit(
            subprocess.call(
                [bun, "run", "build" if args.command == "build" else "validate"],
                cwd=PROJECT,
            )
        )
    children = []
    try:
        children.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "levi.service:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(args.backend_port),
                ],
                cwd=PROJECT,
            )
        )
        children.append(
            subprocess.Popen(
                [
                    bun,
                    "--bun",
                    "run",
                    "dev" if args.command == "dev" else "start",
                    "--hostname",
                    args.host,
                    "--port",
                    str(args.port),
                ],
                cwd=PROJECT,
            )
        )
        print(f"LEVI: http://127.0.0.1:{args.port}", flush=True)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
        code = next(
            (child.returncode for child in children if child.returncode is not None), 0
        )
        if code:
            raise SystemExit(code)
    except KeyboardInterrupt:
        pass
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
