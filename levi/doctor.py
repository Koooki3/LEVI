"""``levi doctor``: is this machine ready to run LEVI, and what is missing.

Read only. It creates nothing (not even the workspace), reads no secret (a
token or a setting is reported as present or absent, never its value), and
connects to nothing but a local model server's health route on the loopback
interface (Ollama, vLLM). LEVI's own ports are looked up in the kernel's
socket table, never connected to; the robot ports 5000 and 8000 are never
touched.

Every check is ``{"id", "group", "level", "message", "fix", "human"}``:

- ``level``: ``ok``; ``info`` (an optional part that is not set up);
  ``warn`` (works, but something is off); ``fail`` (a required part is
  missing or broken);
- ``human``: the fix needs a person (root, a token, a licence, a choice).

The exit code (and ``status`` in ``--json``): 0 ``ok``; 1 ``warn``; 2
``fail`` (a failure an agent can fix: run the named command); 10
``human`` (every remaining failure or warning needs a person).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .paths import PROJECT, ROOT

EXIT = {"ok": 0, "warn": 1, "fail": 2, "human": 10}
SCHEMA = "levi.doctor.v1"
# Inputs of the production build: a change to any of them makes .next stale.
BUILD_INPUTS = (
    "src",
    "package.json",
    "bun.lock",
    "next.config.ts",
    "tsconfig.json",
    "postcss.config.mjs",
)
BUILD_STAMP = ".next/levi-source.sha256"
SOCKET_LIMIT = 103  # bytes in a Unix socket path, NUL included
NEVER_CONNECT = (5000, 8000)  # robot server and policy server ports
POOL_SETTINGS = ("LEVI_POOL_ROOTS", "LEVI_EXPORT_ROOTS", "LEVI_POOL_HELDOUT")


def check(id_, group, level, message, fix="", human=False) -> dict:
    return {
        "id": id_,
        "group": group,
        "level": level,
        "message": message,
        "fix": fix,
        "human": bool(human),
    }


def _run(argv, timeout=10) -> str | None:
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (done.stdout or done.stderr or "").strip() if done.returncode == 0 else None


def source_hash(project: Path | None = None) -> str:
    """One digest of every input of the production build (paths and bytes)."""
    project = project or PROJECT
    digest = hashlib.sha256()
    files = []
    for name in BUILD_INPUTS:
        path = project / name
        if path.is_dir():
            files += [p for p in path.rglob("*") if p.is_file()]
        elif path.is_file():
            files.append(path)
    for path in sorted(files):
        digest.update(str(path.relative_to(project)).encode() + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def write_build_stamp(project: Path | None = None) -> None:
    """Called by ``levi build`` after a successful build."""
    project = project or PROJECT
    stamp = project / BUILD_STAMP
    if stamp.parent.is_dir():
        stamp.write_text(source_hash(project) + "\n")


def listening_ports() -> set:
    from .live.gpumgr import listening_ports as ports

    return ports()


# --- the checks -------------------------------------------------------------------


def system_checks() -> list:
    out = []
    version = sys.version_info
    if (3, 11) <= version[:2] < (3, 14):
        out.append(
            check("python", "system", "ok", f"Python {platform.python_version()}")
        )
    else:
        out.append(
            check(
                "python",
                "system",
                "fail",
                f"Python {platform.python_version()} (LEVI needs 3.11-3.13)",
                "uv python install 3.11 && uv sync --locked",
            )
        )
    uv = shutil.which("uv")
    out.append(
        check("uv", "system", "ok", _run(["uv", "--version"]) or "uv found")
        if uv
        else check(
            "uv",
            "system",
            "fail",
            "uv is not on PATH",
            "install uv: https://docs.astral.sh/uv/getting-started/installation/",
            human=True,
        )
    )
    for tool in ("ffmpeg", "ffprobe"):
        out.append(
            check(tool, "system", "ok", f"{tool} found")
            if shutil.which(tool)
            else check(
                tool,
                "system",
                "fail",
                f"{tool} is not on PATH: conversion and raw-capture views are unavailable",
                "sudo apt install ffmpeg (or put static ffmpeg and ffprobe binaries on PATH)",
                human=True,
            )
        )
    if platform.system() != "Linux":
        out.append(
            check(
                "platform",
                "system",
                "warn",
                f"{platform.system()} {platform.machine()}: only Linux x86_64 is tested "
                "(process tracking and the GPU guard read /proc)",
            )
        )
    else:
        out.append(check("platform", "system", "ok", f"Linux {platform.machine()}"))
    return out


def python_checks() -> list:
    out = []
    agent = all(importlib.util.find_spec(m) for m in ("pydantic_ai", "mcp"))
    out.append(
        check("extra-agent", "python", "ok", "agent extra installed (MCP, model runs)")
        if agent
        else check(
            "extra-agent",
            "python",
            "info",
            "agent extra not installed: no MCP connection, no model-run tasks",
            "uv sync --locked --inexact --extra agent",
        )
    )
    droid = importlib.util.find_spec("h5py") is not None
    out.append(
        check("extra-droid", "python", "ok", "droid extra installed")
        if droid
        else check(
            "extra-droid",
            "python",
            "info",
            "droid extra not installed (only needed to browse DROID raw folders)",
            "uv sync --locked --inexact --extra droid",
        )
    )
    return out


def frontend_checks() -> list:
    from .bootstrap import BUN_VERSION, bun_path

    out = []
    try:
        local = bun_path()
    except RuntimeError as exc:
        return [check("bun", "frontend", "fail", str(exc), human=True)]
    bun = str(local) if local.exists() else shutil.which("bun")
    found = _run([bun, "--version"]) if bun else None
    if not bun:
        out.append(
            check(
                "bun",
                "frontend",
                "fail",
                "Bun is not installed",
                "uv run levi setup",
            )
        )
    elif found != BUN_VERSION:
        out.append(
            check(
                "bun",
                "frontend",
                "warn",
                f"Bun {found or '?'} at {bun}; LEVI pins {BUN_VERSION}",
                "uv run levi setup (installs the pinned Bun into .runtime/)",
            )
        )
    else:
        out.append(check("bun", "frontend", "ok", f"Bun {found}"))
    modules = PROJECT / "node_modules/next/package.json"
    out.append(
        check("frontend-deps", "frontend", "ok", "frontend dependencies installed")
        if modules.is_file()
        else check(
            "frontend-deps",
            "frontend",
            "fail",
            "frontend dependencies are not installed",
            "uv run levi setup",
        )
    )
    build = PROJECT / ".next/BUILD_ID"
    stamp = PROJECT / BUILD_STAMP
    if not build.is_file():
        out.append(
            check(
                "build",
                "frontend",
                "fail",
                "no production build (.next): `levi serve` refuses to start",
                "uv run levi build",
            )
        )
    elif not stamp.is_file():
        out.append(
            check(
                "build",
                "frontend",
                "warn",
                "the production build has no source stamp (built before this "
                "check existed): whether it matches the sources is unknown",
                "uv run levi build (then restart the service)",
            )
        )
    elif stamp.read_text().strip() != source_hash():
        out.append(
            check(
                "build",
                "frontend",
                "warn",
                "the production build is stale: the frontend sources changed since it was built",
                "uv run levi build (then restart the service)",
            )
        )
    else:
        out.append(
            check("build", "frontend", "ok", "production build matches the sources")
        )
    return out


def service_checks(ui_port: int, api_port: int) -> list:
    out = []
    listening = listening_ports()
    for name, port in (("ui-port", ui_port), ("api-port", api_port)):
        if port in listening:
            out.append(
                check(
                    name,
                    "service",
                    "warn",
                    f"port {port} is in use (a running LEVI, or another program)",
                    "if it is not your LEVI, choose another port: levi serve "
                    f"--{'port' if name == 'ui-port' else 'backend-port'} N",
                )
            )
        else:
            out.append(check(name, "service", "ok", f"port {port} is free"))
    return out


def workspace_checks() -> list:
    out = []
    ws = ROOT
    explicit = bool(os.environ.get("LEVI_WORKSPACE"))
    where = f"{ws} ({'LEVI_WORKSPACE' if explicit else 'default: checkout .state'})"
    probe = ws
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    if not os.access(probe, os.W_OK | os.X_OK):
        out.append(
            check(
                "workspace",
                "workspace",
                "fail",
                f"workspace {where} cannot be written",
                "set LEVI_WORKSPACE to a folder you can write",
            )
        )
    else:
        state = "exists" if ws.is_dir() else "will be created on first start"
        out.append(check("workspace", "workspace", "ok", f"workspace {where}, {state}"))
    sock = ws / "outputs/LEVI/workbench/agent/core/api.sock"
    size = len(os.fsencode(sock))
    if size > SOCKET_LIMIT:
        out.append(
            check(
                "socket-path",
                "workspace",
                "fail",
                f"the workspace path is too long for the core's Unix socket ({size} > {SOCKET_LIMIT} bytes)",
                "choose a shorter LEVI_WORKSPACE",
            )
        )
    else:
        out.append(
            check("socket-path", "workspace", "ok", f"core socket path {size} bytes")
        )
    usage = shutil.disk_usage(probe)
    free_gib = round(usage.free / 1024**3, 1)
    out.append(
        check("disk", "workspace", "ok", f"{free_gib} GiB free under the workspace")
        if free_gib >= 20
        else check(
            "disk",
            "workspace",
            "warn",
            f"only {free_gib} GiB free under the workspace",
            "free space, or move LEVI_WORKSPACE",
        )
    )
    env_file = PROJECT / ".env"
    out.append(
        check("env-file", "workspace", "ok", ".env present (values not read)")
        if env_file.is_file()
        else check(
            "env-file",
            "workspace",
            "info",
            "no .env (optional; .env.example lists every setting)",
            "cp .env.example .env",
        )
    )
    # Training pool: present or absent, never the value.
    present = {name: bool(os.environ.get(name)) for name in POOL_SETTINGS}
    shown = ", ".join(f"{n} {'set' if v else 'unset'}" for n, v in present.items())
    if present["LEVI_POOL_ROOTS"] and not present["LEVI_POOL_HELDOUT"]:
        out.append(
            check(
                "pool",
                "workspace",
                "warn",
                f"training pool: {shown}; exports are refused until LEVI_POOL_HELDOUT "
                "names the held-out lists (or `none`)",
                "a person decides which episodes are held out; set LEVI_POOL_HELDOUT in .env",
                human=True,
            )
        )
    else:
        out.append(
            check(
                "pool",
                "workspace",
                "ok" if present["LEVI_POOL_ROOTS"] else "info",
                f"training pool: {shown}"
                + ("" if present["LEVI_POOL_ROOTS"] else " (the pool is idle)"),
            )
        )
    return out


def _worker(id_, python: Path, setup: str, extra: dict | None = None) -> dict:
    if python.is_file() and os.access(python, os.X_OK):
        return check(
            id_, "workers", "ok", f"{id_} worker environment ready", **(extra or {})
        )
    return check(
        id_,
        "workers",
        "info",
        f"{id_} worker not installed (optional)",
        setup,
    )


def worker_checks() -> list:
    out = []
    seg = Path(
        os.environ.get(
            "LEVI_SEG_WORKER_PYTHON",
            PROJECT / "integrations/segmentation/.venv/bin/python",
        )
    )
    out.append(
        _worker(
            "segmentation",
            seg,
            "uv run levi install --profile segmentation (integrations/segmentation/setup.sh)",
        )
    )
    sam3 = Path(
        os.environ.get(
            "LEVI_SAM3_WORKER_PYTHON", PROJECT / "integrations/sam3/.venv/bin/python"
        )
    )
    out.append(_worker("sam3", sam3, "uv run levi install --profile sam3"))
    if sam3.is_file():
        weights = Path(
            os.environ.get("LEVI_SAM3_CHECKPOINT")
            or Path(
                os.environ.get("LEVI_SAM3_CHECKPOINT_DIR") or ROOT / "checkpoints/sam3"
            )
            / os.environ.get("LEVI_SAM3_MODEL_FILENAME", "sam3.pt")
        )
        out.append(
            check("sam3-weights", "workers", "ok", "SAM3 weights present")
            if weights.is_file()
            else check(
                "sam3-weights",
                "workers",
                "warn",
                "SAM3 weights are not downloaded",
                "a person signs in to Hugging Face, accepts the SAM licence and "
                "downloads them in the web UI (object annotation panel)",
                human=True,
            )
        )
    recap = Path(
        os.environ.get(
            "LEVI_RECAP_VALUE_WORKER_PYTHON",
            PROJECT / "integrations/recap_value/.venv/bin/python",
        )
    )
    out.append(
        _worker(
            "recap",
            recap,
            "uv run levi install --profile recap (integrations/recap_value/setup.sh)",
        )
    )
    if recap.is_file():
        folder = ROOT / "checkpoints/recap_value"
        has = folder.is_dir() and any(folder.glob("*/manifest.json"))
        out.append(
            check(
                "recap-checkpoint",
                "workers",
                "ok",
                "a RECAP value checkpoint is imported",
            )
            if has
            else check(
                "recap-checkpoint",
                "workers",
                "info",
                "no RECAP value checkpoint imported (none ships with LEVI)",
                "uv run levi recap --help (import one you have)",
                human=True,
            )
        )
    pilot = PROJECT / "integrations/pilot/node_modules"
    if pilot.is_dir():
        node = _run(["node", "--version"])
        major = int(re.sub(r"\D", "", (node or "v0").split(".")[0]) or 0)
        clis = [c for c in ("codex", "claude") if shutil.which(c)]
        if major >= 22 and clis:
            out.append(
                check(
                    "pilot", "workers", "ok", f"Managed Pilot ready ({', '.join(clis)})"
                )
            )
        else:
            out.append(
                check(
                    "pilot",
                    "workers",
                    "warn",
                    "Managed Pilot adapters installed, but "
                    + (
                        "Node.js >= 22 is missing"
                        if major < 22
                        else "no codex/claude CLI on PATH"
                    ),
                    "install Node.js 22+ and the codex or claude CLI",
                    human=True,
                )
            )
    else:
        out.append(
            check(
                "pilot",
                "workers",
                "info",
                "Managed Pilot adapters not installed (optional)",
                "uv run levi install --profile pilot",
            )
        )
    hf = (
        bool(os.environ.get("HF_TOKEN"))
        or (ROOT / ".cache/huggingface/token").is_file()
    )
    out.append(
        check(
            "hf-token",
            "workers",
            "ok" if hf else "info",
            "Hugging Face token present (not read)"
            if hf
            else "no Hugging Face token (only needed for SAM3 weights, private or gated repositories, uploads)",
            ""
            if hf
            else f'HF_HOME="{ROOT}/.cache/huggingface" hf auth login, or HF_TOKEN',
            human=not hf,
        )
    )
    return out


def gpu_checks() -> list:
    if os.environ.get("LEVI_CPU_ONLY") == "1":
        return [check("gpu", "gpu", "info", "LEVI_CPU_ONLY=1: GPU features are off")]
    if not shutil.which("nvidia-smi"):
        return [
            check(
                "gpu",
                "gpu",
                "info",
                "nvidia-smi not found: no NVIDIA GPU features (local models, SAM3, "
                "segmentation, RECAP run on the GPU)",
                "install the NVIDIA driver (a person, with root)",
                human=True,
            )
        ]
    rows = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.free,driver_version",
            "--format=csv,noheader,nounits",
        ]
    )
    if not rows:
        return [
            check(
                "gpu",
                "gpu",
                "warn",
                "nvidia-smi is installed but does not answer (driver not loaded?)",
                "check the NVIDIA driver (a person)",
                human=True,
            )
        ]
    out = []
    for i, row in enumerate(rows.splitlines()):
        parts = [p.strip() for p in row.split(",")]
        if len(parts) >= 4:
            name, total, free, driver = parts[:4]
            out.append(
                check(
                    f"gpu{i}",
                    "gpu",
                    "ok",
                    f"GPU {i}: {name}, {total} MiB ({free} MiB free), driver {driver}",
                )
            )
    return out or [check("gpu", "gpu", "warn", "nvidia-smi output not understood")]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A probe answers for the port it asked, never for where a redirect points
    (which could be a robot port)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get(port: int, path: str, timeout=2.0):
    if port in NEVER_CONNECT:
        raise ValueError(f"port {port} is never connected to")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(f"http://127.0.0.1:{int(port)}{path}", timeout=timeout) as r:
        return r.status, r.read(65536)


def model_server_checks(ollama_ports, vllm_ports) -> list:
    out = []
    has_ollama = shutil.which("ollama") is not None
    answered = []
    for port in ollama_ports:
        try:
            status, body = _get(port, "/api/version")
            if status == 200:
                answered.append(f"{port} (Ollama {json.loads(body).get('version')})")
        except (OSError, ValueError):
            continue
    if answered:
        out.append(
            check("ollama", "models", "ok", "Ollama answers on " + ", ".join(answered))
        )
    else:
        out.append(
            check(
                "ollama",
                "models",
                "ok" if has_ollama else "info",
                "ollama installed (LEVI starts its own instance on demand)"
                if has_ollama
                else "Ollama not installed (optional local models)",
                "" if has_ollama else "docs/OLLAMA.md section 1 (a person installs it)",
                human=not has_ollama,
            )
        )
    for port in vllm_ports:
        try:
            status, _ = _get(port, "/health")
            _, body = _get(port, "/v1/models")
            names = [m.get("id") for m in json.loads(body).get("data", [])]
            out.append(
                check(
                    f"vllm-{port}",
                    "models",
                    "ok" if status == 200 else "warn",
                    f"a local OpenAI-compatible server on {port} serves {', '.join(map(str, names)) or 'no model'}",
                )
            )
        except ValueError as exc:
            out.append(check(f"vllm-{port}", "models", "warn", str(exc)))
        except urllib.error.HTTPError as exc:
            out.append(
                check(
                    f"vllm-{port}",
                    "models",
                    "warn",
                    f"port {port} answered HTTP {exc.code} (not followed): not a model server?",
                )
            )
        except OSError:
            out.append(
                check(
                    f"vllm-{port}",
                    "models",
                    "info",
                    f"no local model server answers on {port} (optional; docs/VLLM.md)",
                )
            )
    return out


def live_checks() -> list:
    """The live service's configuration, when this machine has one: the one
    LEVI_LIVE_WORKSPACE / LEVI_LIVE_CONFIG names, or the last service's."""
    from .live import cli as live_cli
    from .live import config as live_config

    named = (
        os.environ.get("LEVI_LIVE_WORKSPACE")
        or os.environ.get("LEVI_LIVE_CONFIG")
        or live_cli.remembered_workspace(
            os.environ.get("LEVI_LIVE_HOME") or live_config.DEFAULT_HOME
        )
    )
    if not named:
        return [
            check(
                "live",
                "live",
                "info",
                "no live annotation service configured (optional; docs/LIVE.md)",
                "uv run levi install --profile live",
            )
        ]
    try:
        args = live_cli.build_parser().parse_args(["doctor"])
        config = live_cli.resolve_config(args)
    except ValueError as exc:
        return [check("live", "live", "fail", str(exc), "fix live.toml (docs/LIVE.md)")]
    out = [
        check(
            "live",
            "live",
            "ok",
            f"live configuration {config.path or config.workspace}",
        )
    ]
    for c in live_config.checks(config, watching=True):
        out.append(
            check(
                f"live:{c['key']}",
                "live",
                c["level"],
                c["message"],
                c["fix"],
                # Which folder the evaluation client writes is a person's to say.
                human=c["key"] == "watch.roots" and c["level"] != "ok",
            )
        )
    return out


def run(
    *, ui_port=7860, api_port=7861, ollama_ports=(11435, 11434), vllm_ports=(8100,)
) -> dict:
    checks = []
    for part in (
        system_checks,
        python_checks,
        frontend_checks,
        lambda: service_checks(ui_port, api_port),
        workspace_checks,
        worker_checks,
        gpu_checks,
        lambda: model_server_checks(ollama_ports, vllm_ports),
        live_checks,
    ):
        try:
            checks += part()
        except Exception as exc:  # noqa: BLE001 - one broken probe must not hide the rest
            checks.append(check("doctor", "doctor", "warn", f"a check failed: {exc!r}"))
    status = verdict(checks)
    return {
        "schema": SCHEMA,
        "status": status,
        "exit_code": EXIT[status],
        "checks": checks,
    }


def verdict(checks) -> str:
    fails = [c for c in checks if c["level"] == "fail"]
    warns = [c for c in checks if c["level"] == "warn"]
    if any(not c["human"] for c in fails):
        return "fail"
    if fails or any(c["human"] for c in warns):
        return "human"
    return "warn" if warns else "ok"


MARK = {"ok": "ok  ", "info": "--  ", "warn": "WARN", "fail": "FAIL"}


def render(report) -> str:
    lines = []
    group = None
    for c in report["checks"]:
        if c["group"] != group:
            group = c["group"]
            lines.append(f"[{group}]")
        line = f"  {MARK.get(c['level'], c['level'])} {c['message']}"
        if c["fix"] and c["level"] != "ok":
            line += f"\n         fix{' (a person)' if c['human'] else ''}: {c['fix']}"
        lines.append(line)
    lines.append(f"status: {report['status']} (exit {report['exit_code']})")
    return "\n".join(lines)


def _ports(text: str) -> tuple:
    if text.strip().lower() in ("", "none", "off"):
        return ()
    ports = tuple(int(p) for p in text.split(",") if p.strip())
    bad = [p for p in ports if p in NEVER_CONNECT]
    if bad:
        raise argparse.ArgumentTypeError(f"ports {bad} are never connected to")
    return ports


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="levi doctor",
        description="Check that this machine is ready to run LEVI (read only). "
        "Exit 0 ok, 1 warnings, 2 a failure an agent can fix, 10 the rest needs a person.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--port", type=int, default=7860, help="web UI port to check")
    parser.add_argument(
        "--backend-port", type=int, default=7861, help="API port to check"
    )
    parser.add_argument(
        "--ollama-ports",
        type=_ports,
        default=(11435, 11434),
        help="Ollama ports to ask /api/version (comma separated, 'none' to skip)",
    )
    parser.add_argument(
        "--vllm-ports",
        type=_ports,
        default=(8100,),
        help="local OpenAI-compatible server ports to ask /health and /v1/models "
        "('none' to skip; 5000 and 8000 are refused)",
    )
    args = parser.parse_args(argv)
    report = run(
        ui_port=args.port,
        api_port=args.backend_port,
        ollama_ports=args.ollama_ports,
        vllm_ports=args.vllm_ports,
    )
    print(json.dumps(report, indent=1) if args.json else render(report))
    return report["exit_code"]
