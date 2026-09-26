"""``levi recap``: the RECAP value checkpoint store and value/advantage runs.

levi recap import <src> --name <n> [manifest options]
levi recap import --fake --name <n>
levi recap set <n> [manifest options]
levi recap checkpoints [--verify] [--json]
levi recap run <repo_id> --checkpoint <n> [--episodes 0,3] [--threshold X]
levi recap show <repo_id> [--episode N]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _views(text: str) -> dict:
    """``base=observation.images.view1,left_wrist=observation.images.hand,right_wrist=none``."""
    slots = {
        "base": "base_0_rgb",
        "left_wrist": "left_wrist_0_rgb",
        "right_wrist": "right_wrist_0_rgb",
    }
    out = {}
    for part in filter(None, (p.strip() for p in text.split(","))):
        key, _, value = part.partition("=")
        slot = slots.get(key.strip(), key.strip())
        if slot not in slots.values():
            raise argparse.ArgumentTypeError(f"unknown view slot {key!r}")
        value = value.strip()
        out[slot] = None if value.lower() in ("", "none", "null") else value
    return out


def _manifest_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("manifest fields")
    group.add_argument("--variant", dest="critic_expert_variant")
    group.add_argument("--views", type=_views, help="base=KEY,left_wrist=KEY|none,…")
    group.add_argument("--max-token-len", dest="max_token_len", type=int)
    group.add_argument("--precision", choices=["bfloat16", "float32"])
    group.add_argument("--num-bins", dest="num_bins", type=int)
    group.add_argument("--v-min", dest="v_min", type=float)
    group.add_argument("--v-max", dest="v_max", type=float)
    group.add_argument("--return-min", dest="return_min", type=float)
    group.add_argument("--return-max", dest="return_max", type=float)
    group.add_argument("--gamma", type=float)
    group.add_argument("--failure-reward", dest="failure_reward", type=float)
    group.add_argument("--lookahead", type=int)
    group.add_argument("--positive-quantile", dest="positive_quantile", type=float)
    group.add_argument("--unified-threshold", dest="unified_threshold", type=float)
    group.add_argument("--siglip", help="SigLIP2 so400m-patch14-224 folder")
    group.add_argument("--gemma3", help="Gemma3 270M folder")
    group.add_argument("--tokenizer", help="Gemma3 tokenizer folder")
    group.add_argument("--step", type=int)
    group.add_argument("--notes")


def _fields(args) -> dict:
    keys = (
        "critic_expert_variant",
        "views",
        "max_token_len",
        "precision",
        "num_bins",
        "v_min",
        "v_max",
        "return_min",
        "return_max",
        "gamma",
        "failure_reward",
        "lookahead",
        "positive_quantile",
        "unified_threshold",
        "step",
        "notes",
    )
    fields = {k: getattr(args, k) for k in keys if getattr(args, k) is not None}
    base = {
        k: getattr(args, k)
        for k in ("siglip", "gemma3", "tokenizer")
        if getattr(args, k) is not None
    }
    if base:
        fields["base_models"] = base
    return fields


def _print(value, as_json: bool = True) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="levi recap",
        description="RECAP value model: checkpoints, per-frame values and advantage labels",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="copy a checkpoint into the store")
    imp.add_argument("source", nargs="?", type=Path)
    imp.add_argument("--name", required=True)
    imp.add_argument(
        "--fake", action="store_true", help="a fake checkpoint (no weights)"
    )
    imp.add_argument(
        "--no-inspect",
        action="store_true",
        help="do not read the weights with the worker",
    )
    _manifest_options(imp)
    edit = sub.add_parser("set", help="edit a checkpoint's manifest fields")
    edit.add_argument("name")
    _manifest_options(edit)
    listing = sub.add_parser("checkpoints", help="list checkpoints and readiness")
    listing.add_argument("--verify", action="store_true", help="re-hash the weights")
    run = sub.add_parser("run", help="compute values and advantage labels")
    run.add_argument("repo_id")
    run.add_argument("--checkpoint", required=True)
    run.add_argument("--episodes", help="comma-separated episode indices")
    run.add_argument("--lookahead", type=int)
    run.add_argument("--positive-quantile", type=float)
    run.add_argument("--threshold", type=float)
    run.add_argument("--sft", action="store_true", help="demonstrations: all success")
    show = sub.add_parser("show", help="the current labels of a dataset")
    show.add_argument("repo_id")
    show.add_argument("--episode", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    from . import checkpoints

    args = build_parser().parse_args(argv)
    try:
        if args.command == "import":
            if args.fake and args.source is not None:
                raise ValueError("--fake takes no source")
            if not args.fake and args.source is None:
                raise ValueError("give the checkpoint path (or --fake)")
            _print(
                checkpoints.import_checkpoint(
                    args.source,
                    args.name,
                    provider="fake" if args.fake else "rlinf",
                    fields=_fields(args),
                    inspect=not args.no_inspect,
                )
            )
            return 0
        if args.command == "set":
            _print(checkpoints.update(args.name, _fields(args)))
            return 0
        if args.command == "checkpoints":
            rows = checkpoints.listing()
            if args.verify:
                for row in rows:
                    row["verify"] = checkpoints.verify(row["name"])
            _print(rows)
            return 0
        if args.command == "run":
            return _run(args)
        if args.command == "show":
            from . import jobs

            if args.episode is None:
                _print(
                    {
                        "status": jobs.status(args.repo_id)["current"],
                        "summary": jobs.summary_payload(args.repo_id),
                    }
                )
            else:
                _print(jobs.episode_payload(args.repo_id, args.episode))
            return 0
    except Exception as exc:  # a readable line, not a traceback
        from .jobs import RecapError

        if not isinstance(
            exc, (RecapError, ValueError, KeyError, FileNotFoundError, FileExistsError)
        ):
            raise
        detail = exc.detail if isinstance(exc, RecapError) else exc
        print(f"ERROR: {detail}", file=sys.stderr)
        return 1
    return 2


def _run(args) -> int:
    from . import jobs

    episodes = (
        [int(e) for e in args.episodes.split(",") if e.strip()]
        if args.episodes
        else None
    )
    job = jobs.start(
        args.repo_id,
        args.checkpoint,
        episodes=episodes,
        lookahead=args.lookahead,
        positive_quantile=args.positive_quantile,
        threshold=args.threshold,
        dataset_type="sft" if args.sft else "rollout",
        watch=False,
    )
    print(f"job {job['id']} started ({job['provider']})", file=sys.stderr)
    last = None
    limit = float(__import__("os").getenv("LEVI_RECAP_VALUE_TIMEOUT_SECONDS", "21600"))
    started = time.time()
    try:
        while True:
            job = jobs.collect(jobs.find(job["id"], job["name"]))
            progress = job.get("progress") or {}
            line = f"{job['status']} {progress.get('stage')} {progress.get('done')}/{progress.get('total')}"
            if line != last:
                print(line, file=sys.stderr)
                last = line
            if job["status"] in jobs.FINISHED:
                break
            if time.time() - started > limit:
                job = jobs.cancel(job)
                break
            time.sleep(1)
    except KeyboardInterrupt:
        job = jobs.cancel(jobs.find(job["id"], job["name"]))
    _print(jobs.public(job))
    return 0 if job["status"] == "succeeded" else 1
