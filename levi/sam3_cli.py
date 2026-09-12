"""CPU-safe discovery commands for the SAM3 integration."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .paths import PROJECT


def _enabled() -> bool:
    return os.environ.get("LEVI_SAM3_ENABLED", "1").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def check() -> int:
    integration = PROJECT / "integrations" / "sam3"
    worker = integration / "levi_sam3_worker" / "worker.py"
    python = Path(
        os.environ.get("LEVI_SAM3_WORKER_PYTHON", str(integration / ".venv/bin/python"))
    ).expanduser()
    print("LEVI SAM3 integration")
    print(f"  integration_present={int(integration.is_dir())}")
    print(f"  worker_source_present={int(worker.is_file())}")
    print(f"  worker_python_present={int(python.is_file())}")
    print(f"  LEVI_SAM3_ENABLED={int(_enabled())}")
    print("  model_imported=0")
    print("  cuda_probe_performed=0")
    if not _enabled():
        print("  status=disabled; manual edits remain available")
    elif not python.is_file():
        print("  status=enabled but worker environment is missing")
        return 1
    else:
        print("  status=enabled; the worker will validate checkpoint access at run time")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LEVI SAM3 integration tools")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("check", help="check integration files without importing Torch")
    args = parser.parse_args(argv)
    if args.command in (None, "check"):
        return check()
    parser.error(f"unknown command: {args.command}")
    return 2
