"""``python -m levi.performance trace|bench``: print one JSON document."""

import argparse
import sys

from . import bench, trace


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m levi.performance")
    sub = parser.add_subparsers(dest="command", required=True)
    t = sub.add_parser(
        "trace", help="merge stats.jsonl, gate.jsonl, usage and cost records"
    )
    t.add_argument("--live-dir", help="the live service's <workspace>/live folder")
    t.add_argument("--workspace", help="a LEVI workspace (usage samples, cost records)")
    t.add_argument(
        "--since", type=float, help="epoch seconds; older records are dropped"
    )
    t.add_argument("--limit", type=int, help="keep the newest N episodes")
    t.add_argument(
        "--summary-only", action="store_true", help="omit the per-episode list"
    )
    b = sub.add_parser("bench", help="CPU micro-benchmark on synthetic video")
    b.add_argument("--case", action="append", choices=sorted(bench.CASES))
    b.add_argument("--repeat", type=int, default=3)
    b.add_argument("--frames", type=int, default=182)
    b.add_argument("--size", default="640x480")
    b.add_argument("--fps", type=int, default=10)
    b.add_argument("--cores", type=int, default=bench.MAX_CORES)
    b.add_argument(
        "--scratch", help="folder for the synthetic video (removed afterwards)"
    )
    args = parser.parse_args(argv)
    if args.command == "trace":
        if args.live_dir is None and args.workspace is None:
            parser.error("give --live-dir and/or --workspace")
        result = trace.build(args.live_dir, args.workspace, args.since, args.limit)
        if args.summary_only:
            result.pop("episodes", None)
        print(bench.dumps(result))
        return 0
    result = bench.run(
        args.case,
        args.repeat,
        args.frames,
        args.size,
        args.fps,
        args.scratch,
        args.cores,
    )
    print(bench.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
