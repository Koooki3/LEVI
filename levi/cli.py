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


def namespace_cli(argv) -> int:
    """``levi namespace``: isolated experiments over one registered dataset."""
    from . import catalog

    parser = argparse.ArgumentParser(
        prog="levi namespace",
        description=(
            "A namespace <dataset>--<name> reads the dataset's one source folder "
            "and keeps its own annotations, runs, memory and records."
        ),
    )
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("create", help="create (or show) a namespace")
    create.add_argument("dataset")
    create.add_argument("namespace")
    listing = sub.add_parser("list", help="namespaces of a dataset, or all")
    listing.add_argument("dataset", nargs="?")
    remove = sub.add_parser(
        "remove", help="unregister a namespace (its files on disk stay)"
    )
    remove.add_argument("name", help="<dataset>--<namespace>")
    args = parser.parse_args(argv)
    try:
        if args.action == "create":
            print(
                json.dumps(
                    catalog.create_namespace(args.dataset, args.namespace), indent=1
                )
            )
        elif args.action == "list":
            for item in catalog.namespaces(args.dataset):
                print(item["name"])
        else:
            if not catalog.is_namespace(args.name):
                raise ValueError(f"{args.name!r} is not a namespace")
            catalog.remove_entry(args.name)
            print(f"removed {args.name}; its annotations and records stay on disk")
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


EXIT_SERVING = 3  # levi build refused: a LEVI of this checkout is running


def build_frontend(bun: str, while_serving: bool = False) -> int:
    """``levi build``: install the frontend dependencies first when
    ``node_modules`` does not match ``bun.lock``/``package.json``
    (``bun install --frozen-lockfile``, with the Bun given), then the
    production build and its source stamp. A failed build names the
    dependencies as a likely cause.

    A running LEVI of this checkout (the product's ``next start``, or the
    live service's, which share ``node_modules`` and ``.next``;
    ``install.serving``) refuses the build with ``EXIT_SERVING`` and says
    what to stop. ``while_serving`` builds anyway, with a warning, but never
    reinstalls the dependencies under it."""
    from .bootstrap import frontend_deps_problem, mark_frontend_deps
    from .install import serving

    busy = serving()
    if busy and not while_serving:
        print(
            f"levi build refused: {busy}. `next start` reads node_modules and "
            ".next of this checkout, so stop every LEVI that uses it first: "
            "`uv run --no-sync levi stop` (the product LEVI) and, if the live "
            "service runs from this checkout, `uv run --no-sync levi live stop`; "
            "then `uv run levi build` and start them again. "
            "`levi build --while-serving` builds anyway (no dependency install).",
            file=sys.stderr,
        )
        return EXIT_SERVING
    problem = frontend_deps_problem()
    if busy:
        print(
            f"warning: building while {busy} (--while-serving): the running "
            "page may break until it is restarted"
            + (
                f"; frontend dependencies not installed ({problem}): stop it "
                "and run `uv run levi build`"
                if problem
                else ""
            ),
            file=sys.stderr,
        )
        problem = ""
    if problem:
        print(
            f"frontend dependencies: {problem}; running bun install --frozen-lockfile"
        )
        code = subprocess.call([bun, "install", "--frozen-lockfile"], cwd=PROJECT)
        if code != 0:
            print(
                f"bun install --frozen-lockfile failed (exit {code}): the frontend "
                "dependencies do not match bun.lock. Check the network and "
                "bun.lock, then run: uv run levi setup",
                file=sys.stderr,
            )
            return code
        mark_frontend_deps()
    code = subprocess.call([bun, "run", "build"], cwd=PROJECT)
    if code != 0:
        print(
            f"the production build failed (exit {code}). If it reports a module "
            "that cannot be resolved (for example `Module not found`), the "
            "frontend dependencies are missing or out of date: run "
            "`uv run levi setup` (bun install --frozen-lockfile), then "
            "`uv run levi build` again.",
            file=sys.stderr,
        )
        return code
    # What `levi doctor` compares the sources against (stale builds).
    from .doctor import write_build_stamp

    write_build_stamp()
    return 0


def main():
    if len(sys.argv) > 2 and sys.argv[1:3] == ["dev", "check-contracts"]:
        from .domain.schema_catalog import main as check_contracts

        return check_contracts(sys.argv[3:])
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        # Read only: no configure() (it would create the workspace).
        from .doctor import main as doctor

        return doctor(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "install":
        from .install import main as install

        return install(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "agent":
        from .agent.control import main as agent_control

        return agent_control(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "sam3":
        from .sam3_cli import main as sam3

        sys.argv.pop(1)
        return sam3()
    if len(sys.argv) > 1 and sys.argv[1] == "recap":
        configure()
        from .recap.cli import main as recap

        raise SystemExit(recap(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "export":
        configure()
        from .training_manifest import main as export

        raise SystemExit(export(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "stop":
        from .agent.core import stop

        stopper = argparse.ArgumentParser(
            prog="levi stop",
            description="Stop the shared service and every worker it started. It "
            "refuses while jobs (pool exports, conversions, RECAP or segmentation "
            "runs) are running, and lists them with their progress; use --wait "
            "to let them finish or --force to interrupt them.",
        )
        stopper.add_argument(
            "--all",
            action="store_true",
            help="also stop the Ollama service LEVI started (never a shared one)",
        )
        stopper.add_argument(
            "--force",
            action="store_true",
            help="stop even while jobs run: their workers are killed (a pool export "
            "is kept as interrupted, with its partial output, and can be resumed)",
        )
        stopper.add_argument(
            "--wait",
            nargs="?",
            type=float,
            const=60.0,
            default=None,
            metavar="MINUTES",
            help="wait for running jobs to finish (default 60 minutes), then stop; "
            "refuse if they are still running",
        )
        options = stopper.parse_args(sys.argv[2:])
        result = stop(models=options.all, force=options.force, wait_jobs=options.wait)
        if result.get("status") == "refused":
            from .activity import describe

            print(
                "levi stop refused: these jobs are running and would be killed:\n"
                + describe(result["running"])
                + f"\n{result['hint']}",
                file=sys.stderr,
            )
            print(json.dumps(result, ensure_ascii=False))
            return 3
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "clean":
        from .maintenance import main as clean

        sys.argv.pop(1)
        return clean()
    if len(sys.argv) > 1 and sys.argv[1] == "automatic":
        from .automatic.cli import main as automatic

        raise SystemExit(automatic(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "live":
        from .live.cli import main as live

        raise SystemExit(live(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "pool":
        from .pool.cli import main as pool

        raise SystemExit(pool(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "namespace":
        configure()
        raise SystemExit(namespace_cli(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] in ("sample", "samples"):
        configure()
        from .samples import cli as sample_cli

        raise SystemExit(sample_cli(sys.argv[2:]))
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
            "Also available: doctor (is this machine ready: read-only checks, "
            "--json), install (set up LEVI and optional parts by profile, --plan "
            "--json lists the steps), stop (stop the shared service), clean (bounded "
            "cache cleanup), migrate, convert, agent, sam3, recap (RECAP value "
            "model: checkpoints and advantage labels), export (training manifests: which "
            "frames enter a learner's loss, with what weight), sample (DROID test "
            "samples: `levi sample fetch droid` downloads one), namespace (isolated experiments over one dataset), pool (training pool: "
            "scan read-only pool roots, recipes, merged exports), live (background annotation service for robot rollouts), automatic (automatic evaluation pipeline: checks, dry runs on fakes, status, report), docs (check or regenerate the documentation). Each takes its own "
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
    parser.add_argument(
        "--while-serving",
        action="store_true",
        help="build: build even while a LEVI of this checkout runs (no dependency "
        "install; the running page may break until it is restarted)",
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
        raise SystemExit(build_frontend(bun, args.while_serving))
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
        watch = None
        if args.command == "serve":
            from .agent import core as agent_core
            from .watch import CoreWatch, git_head, restart_veto

            started_at = git_head(PROJECT)

            def veto():
                return restart_veto(PROJECT, started_at, agent_core.orphaned_workers)

            # A core that crashes (not one that `levi stop` stopped) is restarted here.
            watch = CoreWatch(
                agent_core.crashed,
                lambda: agent_core.ensure(args.backend_port),
                veto=veto,
            )
        while all(child.poll() is None for child in children):
            if watch:
                watch.tick()
            time.sleep(0.5)
        code = next(
            (child.returncode for child in children if child.returncode is not None), 0
        )
        if args.command == "serve":
            # The web page ended by itself. Stopped on purpose (SIGTERM or SIGINT, status 0): exit 0, so a
            # supervisor (systemd Restart=on-failure) leaves it. Anything else is a failure and is restarted.
            from .watch import web_exit_status

            raise SystemExit(web_exit_status(code))
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
