"""Single uv entry point supervising frontend and backend."""

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .paths import PROJECT, configure


def frontend_url(host, port):
    """A browser destination, rather than a wildcard listening address."""
    host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"http://{'[' + host + ']' if ':' in host else host}:{port}"


def port_number(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("Port must be between 1 and 65535")
    return port


def check_ports(host, port, backend_port, backend_only=False):
    if not backend_only and port == backend_port:
        raise ValueError("网页与 API 必须使用不同端口 / UI and API ports must differ")
    addresses = [("API", "127.0.0.1", backend_port)]
    if not backend_only:
        addresses.insert(0, ("Web UI", host, port))
    for role, address, number in addresses:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((address, number))
        except OSError as exc:
            raise ValueError(
                f"{role} 无法绑定 / cannot bind {address}:{number}: {exc}. "
                "请检查已有服务或更换端口 / Check existing services or select another port."
            ) from exc


def wait_ready(children, ui_url, api_url, timeout=120):
    """Announce success only after both supervised services answer correctly."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    pending = {"API": api_url + "/api/levi/health", "Web UI": ui_url + "/"}
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        for child in children:
            code = child.poll()
            if code is not None:
                raise RuntimeError(
                    f"LEVI 服务在就绪前退出 / service exited before ready (code {code})"
                )
        for role, url in list(pending.items()):
            try:
                with opener.open(url, timeout=1) as response:
                    if response.status != 200:
                        continue
                    if role == "API":
                        if json.loads(response.read(1024)).get("service") != "levi-api":
                            continue
                    elif "text/html" not in response.headers.get("Content-Type", ""):
                        continue
                    del pending[role]
            except (OSError, ValueError):
                pass
        if pending:
            time.sleep(0.2)
    if pending:
        raise RuntimeError("启动超时 / Startup timed out: " + ", ".join(pending))


def stop_children(children):
    # Bun can start a Node child; stop the entire owned group, including when
    # its launcher has already exited. Never terminate unrelated listeners.
    for child in children:
        try:
            if os.name == "posix":
                os.killpg(child.pid, signal.SIGTERM)
            elif child.poll() is None:
                child.terminate()
        except ProcessLookupError:
            pass
    for child in children:
        try:
            child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(child.pid, signal.SIGKILL)
            else:
                child.kill()
            child.wait()


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
    parser.add_argument(
        "--port",
        type=port_number,
        default=7860,
        help="网页 / Web UI port (default: 7860)",
    )
    parser.add_argument(
        "--backend-port",
        type=port_number,
        default=7861,
        help="内部 API / Internal API port (default: 7861)",
    )
    args = parser.parse_args()
    configure()
    from .bootstrap import bun_path, setup

    if args.command == "setup":
        setup()
        return
    ui_url = frontend_url(args.host, args.port)
    if args.command in ("serve", "dev", "backend"):
        try:
            check_ports(
                args.host, args.port, args.backend_port, args.command == "backend"
            )
        except ValueError as exc:
            parser.error(str(exc))
        os.environ["LEVI_FRONTEND_URL"] = ui_url
    if args.command == "backend":
        import uvicorn

        print(
            "[LEVI] 仅启动 API / API only. 完整网页启动命令 / Start the UI with: uv run levi",
            flush=True,
        )
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
    if args.command == "serve" and not (PROJECT / ".next/BUILD_ID").is_file():
        parser.error("缺少生产构建 / Missing production build. Run: uv run levi build")
    children = []
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print("[LEVI] 正在启动网页与 API / Starting Web UI and API…", flush=True)
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
                start_new_session=(os.name == "posix"),
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
                start_new_session=(os.name == "posix"),
            )
        )
        wait_ready(children, ui_url, os.environ["LEVI_BACKEND_URL"])
        print(
            f"\n[LEVI] 已就绪 / Ready\n"
            f"  网页入口 / Open Web UI: {ui_url}\n"
            f"  内部接口 / API only: 127.0.0.1:{args.backend_port}\n"
            "  请在浏览器打开网页入口；API 端口不是数据浏览页面。\n"
            "  Open the Web UI above; the API port does not serve the workbench.\n",
            flush=True,
        )
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
        code = next(
            (child.returncode for child in children if child.returncode is not None), 0
        )
        if code:
            raise SystemExit(code)
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        print(f"[LEVI] {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        stop_children(children)
