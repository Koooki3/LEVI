"""``levi install``: set LEVI up on this machine, profile by profile.

    levi install --profile core [--profile agent ...] [--plan] [--json] [--yes]

Each profile is a list of steps. A step is either done by LEVI (``auto``:
``uv sync``, Bun and the frontend dependencies, the production build, a
``.env`` copied from ``.env.example`` when there is none, the workspace, a
worker's ``setup.sh``) or handed to a person (``human``: root, a token, a
licence, a choice such as which datasets an agent may see). Every step
knows whether it is already done, so running it again changes nothing that
is in place (idempotent).

``--plan`` only lists the steps (with ``--json``: machine-readable, for an
agent): what each runs, whether it uses the network, roughly how much it
downloads, whether it needs root, a token or a person's consent. Nothing is
created. Without ``--plan`` the automatic steps run (a download over
``CONFIRM_BYTES`` only with ``--yes``), the person's steps are printed as a
list, and ``levi doctor`` runs at the end.

Exit code: 0 everything done; 2 a step failed; 10 steps are left for a
person (or for ``--yes``).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .paths import PROJECT, ROOT

PROFILES = (
    "core",
    "agent",
    "local-model",
    "segmentation",
    "sam3",
    "recap",
    "live",
    "pilot",
)
EXIT_DONE, EXIT_FAILED, EXIT_HUMAN = 0, 2, 10
CONFIRM_BYTES = 1 << 30  # a step that downloads more than this needs --yes
SCHEMA = "levi.install.plan.v1"


@dataclass
class Step:
    id: str
    profile: str
    title: str
    kind: str  # auto | human
    command: list = field(
        default_factory=list
    )  # what runs (auto) or what a person runs
    cwd: str = ""
    network: bool = False
    download_bytes: int = 0  # rough estimate
    sudo: bool = False
    token: bool = False
    consent: bool = False  # a person must agree (a licence, a large download, a choice)
    done: bool = False
    note: str = ""
    docs: str = ""

    def public(self) -> dict:
        row = asdict(self)
        row["download"] = _size(self.download_bytes)
        row["needs_yes"] = self.kind == "auto" and self.download_bytes > CONFIRM_BYTES
        return row


def _size(n: int) -> str:
    if not n:
        return ""
    return f"about {n / 1024**3:.1f} GiB" if n >= 1 << 30 else f"about {n >> 20} MiB"


def _venv_python(folder: str) -> Path:
    return PROJECT / folder / ".venv/bin/python"


def _has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _extras(profiles) -> list:
    extras = []
    if "agent" in profiles or "local-model" in profiles or "live" in profiles:
        extras.append("agent")
    return extras


def _built() -> bool:
    from .doctor import BUILD_STAMP, source_hash

    stamp = PROJECT / BUILD_STAMP
    return (
        (PROJECT / ".next/BUILD_ID").is_file()
        and stamp.is_file()
        and stamp.read_text().strip() == source_hash()
    )


def plan(profiles) -> list:
    """The steps of these profiles, in order, each with ``done`` filled in."""
    profiles = list(dict.fromkeys(["core", *profiles]))
    steps: list = []
    uv = shutil.which("uv")
    if not uv:
        steps.append(
            Step(
                "uv",
                "core",
                "install uv (Python and dependency manager)",
                "human",
                ["curl -LsSf https://astral.sh/uv/install.sh | sh"],
                network=True,
                consent=True,
                docs="https://docs.astral.sh/uv/getting-started/installation/",
            )
        )
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            steps.append(
                Step(
                    tool,
                    "core",
                    f"install {tool} (conversion and raw-capture views)",
                    "human",
                    ["sudo apt install ffmpeg"],
                    network=True,
                    sudo=True,
                    note="or put static ffmpeg/ffprobe binaries on PATH (no root)",
                    docs="INSTALL.md",
                )
            )
            break
    extras = _extras(profiles)
    synced = all(_has(m) for m in ("fastapi", "pandas")) and (
        "agent" not in extras or all(_has(m) for m in ("pydantic_ai", "mcp"))
    )
    steps.append(
        Step(
            "python-deps",
            "core",
            "Python dependencies"
            + (f" with the {', '.join(extras)} extra" if extras else ""),
            "auto",
            ["uv", "sync", "--locked", "--inexact"]
            + [x for e in extras for x in ("--extra", e)],
            cwd=str(PROJECT),
            network=True,
            download_bytes=600 << 20,
            done=synced,
            note="--inexact keeps packages already installed (a developer's dev group)",
        )
    )
    steps.append(
        Step(
            "env-file",
            "core",
            ".env from .env.example (only when there is no .env)",
            "auto",
            ["cp", ".env.example", ".env"],
            cwd=str(PROJECT),
            done=(PROJECT / ".env").exists(),
        )
    )
    steps.append(
        Step(
            "workspace",
            "core",
            f"the workspace {ROOT}",
            "auto",
            done=(ROOT / "outputs/LEVI/workbench").is_dir(),
            note="LEVI_WORKSPACE chooses it (default: the checkout's .state)",
        )
    )
    from .bootstrap import bun_path

    steps.append(
        Step(
            "frontend-deps",
            "core",
            "Bun (pinned, checksum-verified) and the frontend dependencies",
            "auto",
            [sys.executable, "-m", "levi.cli", "setup"],
            cwd=str(PROJECT),
            network=True,
            download_bytes=450 << 20,
            done=bun_path().exists()
            and (PROJECT / "node_modules/next/package.json").is_file(),
        )
    )
    steps.append(
        Step(
            "build",
            "core",
            "the production build of the web UI (a few minutes)",
            "auto",
            [sys.executable, "-m", "levi.cli", "build"],
            cwd=str(PROJECT),
            done=_built(),
            note="rebuilt when the frontend sources changed (levi doctor: build)",
        )
    )
    if "agent" in profiles:
        steps.append(
            Step(
                "agent-connect",
                "agent",
                "connect your agent (Claude Code, Codex) to the datasets it may use",
                "human",
                [
                    (
                        "uv run levi agent connect --client claude|codex --project <dir> "
                        "--dataset local/<name> --apply"
                    )
                ],
                consent=True,
                note="which datasets an agent sees is a person's choice",
                docs="docs/AGENTS.md",
            )
        )
    if "local-model" in profiles:
        steps.append(
            Step(
                "local-model-server",
                "local-model",
                "a local model server: Ollama (small models) or vLLM (docs/VLLM.md)",
                "human",
                ["see docs/OLLAMA.md section 1, or docs/VLLM.md section 1"],
                network=True,
                download_bytes=4 << 30,
                consent=True,
                done=shutil.which("ollama") is not None
                or (PROJECT / ".venv-vllm/bin/vllm").exists(),
                note="model weights are large; a person chooses the model and accepts its licence",
                docs="docs/OLLAMA.md",
            )
        )
        steps.append(
            Step(
                "local-model-bind",
                "local-model",
                "create and bind a model profile in the web UI (Agent Workbench -> local model)",
                "human",
                consent=True,
                note="binding records which weights the profile trusts; there is no CLI for it yet",
                docs="docs/OLLAMA.md",
            )
        )
    if "segmentation" in profiles:
        steps.append(
            Step(
                "segmentation-worker",
                "segmentation",
                "the fast segmentation worker environment (Python 3.12, Torch CUDA)",
                "auto",
                ["bash", "integrations/segmentation/setup.sh"],
                cwd=str(PROJECT),
                network=True,
                download_bytes=5 << 30,
                done=_venv_python("integrations/segmentation").is_file(),
                note="the RF-DETR base weights download on first use (no token)",
                docs="docs/SEGMENTATION.md",
            )
        )
    if "sam3" in profiles:
        done = _venv_python("integrations/sam3").is_file()
        steps.append(
            Step(
                "sam3-venv",
                "sam3",
                "the SAM3 worker environment (Python 3.12)",
                "auto",
                ["uv", "venv", "--python", "3.12", "integrations/sam3/.venv"],
                cwd=str(PROJECT),
                network=True,
                done=done,
            )
        )
        steps.append(
            Step(
                "sam3-deps",
                "sam3",
                "the SAM3 worker dependencies (Torch CUDA)",
                "auto",
                ["uv", "sync", "--project", "integrations/sam3", "--frozen"],
                cwd=str(PROJECT),
                network=True,
                download_bytes=5 << 30,
                done=done and (PROJECT / "integrations/sam3/.venv/lib").is_dir(),
                docs="docs/SAM3.md",
            )
        )
        steps.append(
            Step(
                "sam3-weights",
                "sam3",
                "SAM3 weights: Hugging Face sign-in, accept the SAM licence, download in the web UI",
                "human",
                [f'HF_HOME="{ROOT}/.cache/huggingface" hf auth login'],
                network=True,
                download_bytes=3 << 30,
                token=True,
                consent=True,
                done=(ROOT / "checkpoints/sam3/sam3.pt").is_file(),
                docs="docs/SAM3.md",
            )
        )
    if "recap" in profiles:
        steps.append(
            Step(
                "recap-worker",
                "recap",
                "the RECAP value-model worker environment",
                "auto",
                ["bash", "integrations/recap_value/setup.sh"],
                cwd=str(PROJECT),
                network=True,
                download_bytes=int(7.2 * (1 << 30)),
                done=_venv_python("integrations/recap_value").is_file(),
                docs="docs/RECAP.md",
            )
        )
        steps.append(
            Step(
                "recap-checkpoint",
                "recap",
                "import a RECAP value checkpoint (none ships with LEVI)",
                "human",
                ["uv run levi recap --help"],
                consent=True,
                docs="docs/RECAP.md",
            )
        )
    if "live" in profiles:
        steps.append(
            Step(
                "vllm-env",
                "live",
                "a vLLM environment and the model weights (docs/VLLM.md section 1)",
                "human",
                [
                    "uv venv --python 3.12 .venv-vllm",
                    'uv pip install --python .venv-vllm/bin/python "vllm==0.30.0"',
                    "hf download RedHatAI/Qwen3.8-27B-INT4",
                ],
                cwd=str(PROJECT),
                network=True,
                download_bytes=27 << 30,
                consent=True,
                done=(PROJECT / ".venv-vllm/bin/vllm").exists(),
                note="needs an NVIDIA driver for the torch CUDA version; about 8 GiB of "
                "packages plus 19 GiB of weights; a person chooses the model",
                docs="docs/VLLM.md",
            )
        )
        steps.append(
            Step(
                "live-config",
                "live",
                "a live.toml naming this machine's rollout folder",
                "human",
                [
                    "uv run levi live init --root /path/to/rollouts",
                    "uv run levi live doctor",
                ],
                consent=True,
                note="which folder the evaluation client writes is the person's to say",
                docs="docs/LIVE.md",
            )
        )
    if "pilot" in profiles:
        steps.append(
            Step(
                "pilot-adapters",
                "pilot",
                "the Managed Pilot adapters",
                "auto",
                [str(bun_path()), "install", "--frozen-lockfile"],
                cwd=str(PROJECT / "integrations/pilot"),
                network=True,
                download_bytes=150 << 20,
                done=(PROJECT / "integrations/pilot/node_modules").is_dir(),
                docs="docs/PILOT.md",
            )
        )
        if not (shutil.which("codex") or shutil.which("claude")) or not shutil.which(
            "node"
        ):
            steps.append(
                Step(
                    "pilot-cli",
                    "pilot",
                    "Node.js 22+ and the codex or claude CLI, signed in",
                    "human",
                    network=True,
                    token=True,
                    consent=True,
                    docs="docs/PILOT.md",
                )
            )
    return steps


def _run_step(step: Step) -> tuple[bool, str]:
    if step.id == "env-file":
        target = PROJECT / ".env"
        if not target.exists():
            shutil.copyfile(PROJECT / ".env.example", target)
        return True, ""
    if step.id == "workspace":
        from .paths import configure

        configure()
        return True, ""
    env = dict(os.environ)
    if step.id == "frontend-deps" or step.id == "build":
        # `levi setup` / `levi build` in a child: the same interpreter.
        env.setdefault("NEXT_TELEMETRY_DISABLED", "1")
    try:
        code = subprocess.call(step.command, cwd=step.cwd or None, env=env)
    except OSError as exc:
        return False, str(exc)
    if code == 0 and step.id == "build":
        from .doctor import write_build_stamp

        write_build_stamp()
    return code == 0, "" if code == 0 else f"exit {code}"


def execute(steps, *, yes=False, log=print) -> dict:
    results = []
    failed = False
    for step in steps:
        row = step.public()
        if step.done:
            row["result"] = "already done"
        elif step.kind == "human":
            row["result"] = "for a person"
        elif failed:
            row["result"] = "skipped (an earlier step failed)"
        elif row["needs_yes"] and not yes:
            row["result"] = f"waiting for --yes (downloads {row['download']})"
        else:
            log(f"[levi install] {step.title} ...", flush=True)
            ok, why = _run_step(step)
            row["result"] = "done" if ok else f"failed: {why}"
            failed = failed or not ok
        results.append(row)
    return {"steps": results, "failed": failed}


def summary_code(results: dict) -> int:
    if results["failed"]:
        return EXIT_FAILED
    pending = [
        r
        for r in results["steps"]
        if r["result"] == "for a person" or r["result"].startswith("waiting for --yes")
    ]
    return EXIT_HUMAN if pending else EXIT_DONE


def render_plan(steps) -> str:
    lines = []
    for s in steps:
        state = "done" if s.done else ("PERSON" if s.kind == "human" else "auto")
        flags = [
            f
            for f, on in (
                ("network", s.network),
                ("sudo", s.sudo),
                ("token", s.token),
                ("consent", s.consent),
            )
            if on
        ]
        lines.append(
            f"[{state:6}] {s.profile}/{s.id}: {s.title}"
            + (
                f"  ({', '.join(flags + ([_size(s.download_bytes)] if s.download_bytes else []))})"
                if flags or s.download_bytes
                else ""
            )
        )
        if s.command and not s.done:
            shown = (
                [" ".join(map(str, s.command))]
                if s.kind == "auto"
                else [str(c) for c in s.command]
            )
            lines += [f"         $ {c}" for c in shown]
        if s.note and not s.done:
            lines.append(f"         note: {s.note}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="levi install",
        description="Install LEVI and optional parts, idempotently. Steps a person "
        "must take are listed, never worked around. Exit 0 done, 2 a step failed, "
        "10 steps are left for a person (or for --yes).",
    )
    parser.add_argument(
        "--profile",
        action="append",
        choices=PROFILES,
        help="what to install (repeatable; core is always included)",
    )
    parser.add_argument(
        "--plan", action="store_true", help="list the steps; change nothing"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--yes",
        action="store_true",
        help=f"allow automatic steps that download more than {CONFIRM_BYTES >> 30} GiB",
    )
    parser.add_argument(
        "--no-doctor", action="store_true", help="do not run levi doctor at the end"
    )
    args = parser.parse_args(argv)
    profiles = args.profile or ["core"]
    steps = plan(profiles)
    if args.plan:
        humans = [s for s in steps if s.kind == "human" and not s.done]
        if args.json:
            print(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "profiles": list(dict.fromkeys(["core", *profiles])),
                        "workspace": str(ROOT),
                        "steps": [s.public() for s in steps],
                    },
                    indent=1,
                )
            )
        else:
            print(render_plan(steps))
        return EXIT_HUMAN if humans else EXIT_DONE

    def log(*parts, **kw):
        # With --json, stdout carries only the JSON document.
        print(*parts, file=sys.stderr if args.json else sys.stdout, **kw)

    results = execute(steps, yes=args.yes, log=log)
    code = summary_code(results)
    doctor = None
    if not args.no_doctor:
        from . import doctor as doctor_module

        doctor = doctor_module.run()
    if args.json:
        print(
            json.dumps(
                {
                    "schema": "levi.install.result.v1",
                    "exit_code": code,
                    **results,
                    "doctor": doctor,
                },
                indent=1,
            )
        )
    else:
        for row in results["steps"]:
            print(f"{row['result']:>34}  {row['profile']}/{row['id']}: {row['title']}")
        people = [r for r in results["steps"] if r["result"] == "for a person"]
        if people:
            print("\nFor a person (LEVI does not do these):")
            for r in people:
                print(f"- {r['title']}")
                for c in r["command"]:
                    print(f"    {c if isinstance(c, str) else ' '.join(c)}")
                if r["docs"]:
                    print(f"    see {r['docs']}")
        if doctor:
            from .doctor import render

            print("\n" + render(doctor))
    return code
