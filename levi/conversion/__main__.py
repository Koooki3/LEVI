"""Run with `uv run levi convert STAGE --source ... --output ...`."""

import argparse
import json

from ..catalog import atomic
from ..paths import configure, inside
from .engine import execute, fingerprint
from .options import STAGES, Options


def main():
    parser = argparse.ArgumentParser(description="LEVI built-in capture pipeline")
    parser.add_argument("stage", choices=STAGES)
    parser.add_argument("--source", required=True)
    parser.add_argument(
        "--output",
        required=True,
        help="New output path, relative to LEVI_WORKSPACE or absolute inside it",
    )
    parser.add_argument(
        "--options",
        help="JSON options file (camera map, thresholds, tasks, exclusions)",
    )
    parser.add_argument("--fps", type=float)
    parser.add_argument("--source-fps", type=float)
    parser.add_argument("--result", help=argparse.SUPPRESS)
    parser.add_argument("--progress", help=argparse.SUPPRESS)
    parser.add_argument("--intermediate", help=argparse.SUPPRESS)
    parser.add_argument("--expected-source", help=argparse.SUPPRESS)
    args = parser.parse_args()
    configure()
    settings = json.loads(inside(args.options).read_text()) if args.options else {}
    if args.fps is not None:
        settings["fps"] = args.fps
    if args.source_fps is not None:
        settings["source_fps"] = args.source_fps
    options = Options.model_validate(settings)
    source = inside(args.source)
    target = inside(args.output)
    before = fingerprint(source)
    if args.expected_source and args.expected_source != before:
        raise ValueError("Capture changed after planning; create a new plan")
    result = execute(
        args.stage,
        source,
        target,
        options,
        inside(args.progress) if args.progress else None,
        inside(args.intermediate) if args.intermediate else None,
    )
    if fingerprint(source) != before:
        raise ValueError("Source changed during conversion; output requires review")
    if args.result:
        atomic(inside(args.result), result)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    if not result.get("ok"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
