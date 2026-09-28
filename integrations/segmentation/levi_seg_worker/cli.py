"""Command line entry point; model imports stay lazy.

    python -m levi_seg_worker.cli --check
    python -m levi_seg_worker.cli label  --plan P --output R [--progress G]
    python -m levi_seg_worker.cli distil --plan P --output R [--progress G]
    python -m levi_seg_worker.cli live   --plan P          (stdin/stdout JSON lines)
    python -m levi_seg_worker.cli bench  --plan P --output R

The ``fake`` provider needs only NumPy, OpenCV and pyarrow, so LEVI runs it
with its own Python for CPU tests; everything else runs in this project's
environment (integrations/segmentation/setup.sh).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LEVI fast segmentation worker")
    parser.add_argument("--check", action="store_true", help="print versions; no model, no CUDA probe")
    parser.add_argument("command", nargs="?", choices=["label", "distil", "live", "bench"])
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--progress", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.check:
        from .student import environment

        print(json.dumps(environment(), indent=2))
        return 0
    if not args.command or not args.plan:
        parser.error("a command and --plan are required unless --check is used")
    if args.command == "live":
        from .live import serve

        return serve(args.plan)
    if not args.output:
        parser.error("--output is required")
    if args.command == "label":
        from .label import run

        return run(args.plan, args.output, args.progress)
    if args.command == "distil":
        from .distil import run

        return run(args.plan, args.output, args.progress)
    from .live import bench

    return bench(args.plan, args.output)


if __name__ == "__main__":
    sys.exit(main())
