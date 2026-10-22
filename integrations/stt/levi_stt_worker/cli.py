"""Command line entry point; model imports stay lazy.

    python -m levi_stt_worker.cli --check
    python -m levi_stt_worker.cli serve --model-dir D [--model N] [--device cuda]
        [--max-seconds 10] [--cpu-affinity 8-15] [--provider qwen3-asr|fake]
    python -m levi_stt_worker.cli install-model --model-dir D [--model N] [--from-dir SNAPSHOT]
    python -m levi_stt_worker.cli verify-model --model-dir D [--model N]

``serve`` speaks the stdin/stdout protocol described in serve.py (LEVI's
service starts it and tracks the process). The ``fake`` provider needs only
the standard library, so LEVI's CPU tests run it with LEVI's own Python;
everything else runs in this project's environment
(integrations/stt/setup.sh).
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib import metadata
from pathlib import Path

from . import __version__
from .common import DEFAULT_MODEL, EXIT_STARTUP, MODEL_NAME, MODELS

REQUIRED = ("numpy", "torch", "transformers", "accelerate", "silero-vad")


def environment() -> dict[str, object]:
    """Versions of what the worker imports, read from package metadata (no
    import, no CUDA probe, no model)."""
    versions: dict[str, str | None] = {}
    for name in REQUIRED:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return {
        "worker": __version__,
        "python": sys.version.split()[0],
        "packages": versions,
        "complete": all(versions.values()),
    }


def _model_name(value: str) -> str:
    if not MODEL_NAME.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "a model is the name of a folder inside --model-dir"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LEVI speech-to-text worker")
    parser.add_argument(
        "--check", action="store_true", help="print versions; no model, no CUDA probe"
    )
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser(
        "serve", help="read requests on stdin, answer on stdout"
    )
    serve.add_argument("--model-dir", type=Path)
    serve.add_argument("--model", type=_model_name, default=DEFAULT_MODEL)
    serve.add_argument("--device", choices=["cuda"], default="cuda")
    serve.add_argument("--max-seconds", type=float, default=10.0)
    serve.add_argument("--cpu-affinity", default="")
    serve.add_argument("--provider", choices=["qwen3-asr", "fake"], default="qwen3-asr")
    install = commands.add_parser(
        "install-model", help="download the pinned model and write MANIFEST.sha256"
    )
    install.add_argument("--model-dir", type=Path, required=True)
    install.add_argument("--model", choices=sorted(MODELS), default=DEFAULT_MODEL)
    install.add_argument(
        "--from-dir", type=Path, help="copy this local snapshot instead of downloading"
    )
    verify = commands.add_parser(
        "verify-model", help="check a model folder against its MANIFEST.sha256"
    )
    verify.add_argument("--model-dir", type=Path, required=True)
    verify.add_argument("--model", type=_model_name, default=DEFAULT_MODEL)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.check:
        report = environment()
        print(json.dumps(report, indent=2))
        return 0 if report["complete"] else 1
    if args.command == "serve":
        if args.provider != "fake" and args.model_dir is None:
            parser.error("--model-dir is required unless --provider fake")
        from .serve import Options, serve

        return serve(
            Options(
                args.provider,
                args.model_dir,
                args.model,
                args.device,
                args.max_seconds,
                args.cpu_affinity,
            )
        )
    if args.command in ("install-model", "verify-model"):
        return _model_command(args)
    parser.error("a command is required unless --check is used")
    return 2


def _model_command(args: argparse.Namespace) -> int:
    from .errors import StartupError
    from .install import install_model
    from .manifest import verify_manifest

    try:
        if args.command == "install-model":
            folder = install_model(args.model_dir, args.model, args.from_dir)
            verified = verify_manifest(folder)
        else:
            folder = args.model_dir / args.model
            verified = verify_manifest(folder)
    except StartupError as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return error.exit_code
    except (OSError, ValueError, ImportError) as error:
        print(f"model install failed: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_STARTUP
    print(f"{args.model}: {len(verified)} files verified in {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
