"""Command line entry point that keeps model imports lazy."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def _enabled() -> bool:
    return os.environ.get("LEVI_SAM3_ENABLED", "1").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LEVI SAM3 worker")
    parser.add_argument("--check", action="store_true", help="check configuration without loading Torch")
    parser.add_argument("--plan", type=Path, help="plan JSON created by LEVI")
    parser.add_argument("--output", type=Path, help="result JSON path")
    args = parser.parse_args(argv)
    if args.check:
        print("LEVI SAM3 worker configuration")
        print(f"  LEVI_SAM3_ENABLED={int(_enabled())}")
        print("  model_imported=0")
        print("  cuda_probe_performed=0")
        if not _enabled():
            print("  status=disabled (set LEVI_SAM3_ENABLED=1 to enable the global integration)")
        else:
            print("  status=enabled (runtime will validate the configured checkpoint and CUDA device)")
        return 0
    if not args.plan or not args.output:
        parser.error("--plan and --output are required unless --check is used")
    if not _enabled():
        parser.error("SAM3 is disabled; set LEVI_SAM3_ENABLED=1 to enable it")
    from .worker import run_plan

    run_plan(args.plan, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
