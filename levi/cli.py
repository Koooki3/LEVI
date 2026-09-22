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
        raise ValueError("UI and API ports must differ / 网页与 API 必须使用不同端口")
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
                f"{role} cannot bind {address}:{number} / 无法绑定: {exc}. "
                "Check existing services or select another port. / 请检查已有服务或更换端口。"
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
                    f"Service exited before ready (code {code}) / LEVI 服务在就绪前退出"
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
        raise RuntimeError("Startup timed out / 启动超时: " + ", ".join(pending))


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
    if len(sys.argv) > 2 and sys.argv[1:3] == ["dev", "check-contracts"]:
        from .domain.schema_catalog import main as check_contracts

        return check_contracts(sys.argv[3:])
    if len(sys.argv) > 1 and sys.argv[1] == "agent":
        from .agent.control import main as agent_control

        return agent_control(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "sam3":
        from .sam3_cli import main as sam3

        sys.argv.pop(1)
        return sam3()
    if len(sys.argv) > 1 and sys.argv[1] == "stop":
        from .agent.core import stop

        print(json.dumps(stop(), ensure_ascii=False))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "clean":
        from .maintenance import main as clean

        sys.argv.pop(1)
        return clean()
    if len(sys.argv) > 1 and sys.argv[1] == "docs":
        from .docs import main as docs

        return docs(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        from .migrations import main as migrate

        sys.argv.pop(1)
        return migrate()
    if len(sys.argv) > 1 and sys.argv[1] == "convert":
        from .conversion.__main__ import main as convert

        sys.argv.pop(1)
        return convert()
    parser = argparse.ArgumentParser(
        description="LEVI · LeRobot dataset workbench",
        # These are dispatched before parsing, so argparse cannot list them and
        # someone reading --help would not know they exist.
        epilog=(
            "Also available: stop (stop the shared service), clean (bounded "
            "cache cleanup), migrate, convert, agent, sam3, docs (check or regenerate "
            "the documentation). Each takes its own "
            "--help."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
        help="Web UI port / 网页端口 (default: 7860)",
    )
    parser.add_argument(
        "--backend-port",
        type=port_number,
        default=7861,
        help="Internal API port / 内部 API 端口 (default: 7861)",
    )
    args = parser.parse_args()
    configure()
    from .bootstrap import bun_path, setup

    if args.command == "setup":
        setup()
        return
    ui_url = frontend_url(args.host, args.port)
    if args.command == "backend":
        from .agent.core import ensure

        print(ensure(args.backend_port))
        return
    if args.command in ("serve", "dev"):
        from .agent.core import directory, ensure

        os.environ["LEVI_FRONTEND_URL"] = ui_url
        core = ensure(args.backend_port)
        args.backend_port = core["port"]
        # The token itself, and where it lives: the frontend re-reads the file
        # so a Core restart does not strand it with a stale credential.
        os.environ["LEVI_CORE_DIR"] = str(directory())
        os.environ["LEVI_UI_TOKEN"] = (directory() / "human.key").read_text()
        os.environ["LEVI_FRONTEND_URL"] = ui_url
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
    if args.command == "build":
        raise SystemExit(subprocess.call([bun, "run", "build"], cwd=PROJECT))
    if args.command == "check":
        # Frontend (type-check/lint/format/tests) and the Python lint, in one
        # command — CI runs both too (see .github/workflows/test.yml); this
        # is the local equivalent so `ruff` drift doesn't go unnoticed
        # between CI runs the way it once did (29 accumulated issues, none
        # caught locally, because nothing ran it here).
        frontend = subprocess.call([bun, "run", "validate"], cwd=PROJECT)
        backend = subprocess.call(["ruff", "check", "."], cwd=PROJECT)
        # And the docs against the code (see levi/docs.py).
        from .docs import main as docs

        raise SystemExit(frontend or backend or docs([]))
    if args.command == "serve" and not (PROJECT / ".next/BUILD_ID").is_file():
        parser.error("Missing production build. Run: uv run levi build / 缺少生产构建")
    os.environ.setdefault("LEVI_UI_TOKEN", __import__("secrets").token_urlsafe(32))
    children = []
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print("[LEVI] Starting Web UI and API… / 正在启动网页与 API…", flush=True)
    try:
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
            f"\n[LEVI] Ready / 已就绪\n"
            f"  Open Web UI / 网页入口: {ui_url}\n"
            f"  API only / 内部接口: 127.0.0.1:{args.backend_port}\n"
            "  Open the Web UI above; the API port does not serve the workbench.\n"
            "  请在浏览器打开网页入口；API 端口不是数据浏览页面。\n",
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


if __name__ == "__main__":
    raise SystemExit(main())
