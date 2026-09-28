"""Command line entry point; model imports stay lazy.

``--plan/--output/--progress`` runs a plan written by LEVI. The ``fake``
provider needs only numpy and pyarrow, so LEVI runs it with its own Python;
``rlinf`` needs this project's environment (setup.sh).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LEVI RECAP value worker")
    parser.add_argument("--plan", type=Path, help="plan JSON written by LEVI")
    parser.add_argument("--output", type=Path, help="result JSON path")
    parser.add_argument("--progress", type=Path, default=None, help="progress JSON")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report versions and whether the transformers patch is applied",
    )
    parser.add_argument(
        "--inspect",
        type=Path,
        metavar="WEIGHTS",
        help="print what a full_weights.pt contains (keys, inferred variant)",
    )
    parser.add_argument(
        "--strict-check",
        type=Path,
        metavar="CHECKPOINT_DIR",
        help="build the model from a checkpoint folder's manifest.json (CPU) and "
        "report missing / unexpected keys of its weights",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="build the model from local test configs, save/strictly reload "
        "random weights and label a synthetic view (no checkpoint needed)",
    )
    parser.add_argument(
        "--size", choices=["tiny", "full"], default="tiny", help="selftest size"
    )
    parser.add_argument(
        "--device", default="both", help="selftest device: cuda, cpu or both"
    )
    parser.add_argument(
        "--frames", type=int, default=0, help="selftest: also time one N-frame episode"
    )
    parser.add_argument("--workdir", type=Path, default=None, help="selftest folder")
    args = parser.parse_args(argv)
    if args.check:
        from .rlinf_provider import environment

        report = environment()
        print(json.dumps(report, indent=2))
        return 0 if report["patch_applied"] else 1
    if args.inspect:
        from .rlinf_provider import inspect_weights

        print(json.dumps(inspect_weights(args.inspect)))
        return 0
    if args.strict_check:
        from .rlinf_provider import strict_report

        report = strict_report(args.strict_check)
        print(json.dumps(report))
        return 0 if report["ok"] else 1
    if args.selftest:
        from .selftest import run

        report = run(
            size=args.size,
            devices=args.device,
            frames=args.frames,
            workdir=args.workdir,
        )
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    if not args.plan or not args.output:
        parser.error("--plan and --output are required")
    from .runner import run_plan

    return run_plan(args.plan, args.output, args.progress)


if __name__ == "__main__":
    sys.exit(main())
