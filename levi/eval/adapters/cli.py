"""``python -m levi.eval.adapters {direct,prior-levi,levi2}``: one record to a
new file. Exit 0 = written; 2 = refused (the reason on stderr, nothing written)."""

from __future__ import annotations

import argparse
import sys

from ..compare import WALL_BASES
from . import common, hub, levi2
from .common import AdapterError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m levi.eval.adapters",
        description=(
            "Turn the files of one arm's run into an arm_record.v2 JSON file. "
            "Reads only the files named, writes only --out (which must not "
            "exist), uses no model, GPU or port."
        ),
    )
    sub = parser.add_subparsers(dest="arm", required=True)

    def common_args(p, *, result_required):
        p.add_argument(
            "--round",
            required=True,
            help="the round declaration (levi.eval.arm_round.v1)",
        )
        p.add_argument(
            "--out", required=True, help="a new file; written as <out>.partial first"
        )
        p.add_argument(
            "--wall-basis",
            required=True,
            choices=WALL_BASES,
            help="does this arm's wall time include queueing? The files do not say",
        )
        p.add_argument(
            "--result", required=result_required, help="the run's result JSON"
        )
        p.add_argument(
            "--quality-path", help="dotted path to the quality in the result"
        )
        p.add_argument(
            "--scored-count-path",
            help="dotted path to the count it was scored over (default: episodes beside it)",
        )
        p.add_argument(
            "--scores", help="a scorer's aggregate file (levi.eval.arm_scores.v1)"
        )

    a = sub.add_parser("direct", help="arm A: levi-vlm-eval direct.py result")
    common_args(a, result_required=True)
    a.add_argument(
        "--no-external-model",
        action="store_true",
        help="state that this arm made no external-model call",
    )
    b = sub.add_parser(
        "prior-levi", help="arm B: levi-vlm-eval drive.py result (+ ledger)"
    )
    common_args(b, result_required=True)
    b.add_argument("--ledger", help="the run's ledger.json (levi.harness.ledger.v1)")
    b.add_argument(
        "--no-external-model",
        action="store_true",
        help="state that this arm made no external-model call",
    )
    c = sub.add_parser(
        "levi2", help="arm C: route log (+ local result, + external-token report)"
    )
    common_args(c, result_required=False)
    c.add_argument("--route-log", required=True, help="JSON lines, one row per problem")
    c.add_argument("--ledger", help="the local run's ledger.json")
    c.add_argument("--external-tokens", help="a levi.eval.token_monitor.v1 report")
    c.add_argument("--external-task", help="the task label to read in that report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        round_ = common.load_round(args.round)
        scores = common.load_scores(args.scores) if args.scores else None
        shared = {
            "wall_basis": args.wall_basis,
            "quality_path": args.quality_path,
            "scored_count_path": args.scored_count_path,
            "scores": scores,
        }
        if args.arm == "direct":
            row, notes = hub.direct(
                args.result, round_, no_external_model=args.no_external_model, **shared
            )
        elif args.arm == "prior-levi":
            row, notes = hub.prior_levi(
                args.result,
                round_,
                ledger_path=args.ledger,
                no_external_model=args.no_external_model,
                **shared,
            )
        else:
            row, notes = levi2.levi2(
                args.route_log,
                round_,
                result_path=args.result,
                ledger_path=args.ledger,
                external_path=args.external_tokens,
                external_task=args.external_task,
                **shared,
            )
        path = common.write(row, args.out)
    except AdapterError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    print(f"wrote {path}")
    return 0
