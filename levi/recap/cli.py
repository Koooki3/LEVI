"""``levi recap``: the RECAP value checkpoint store and value/advantage runs.

levi recap import <src> --name <n> [--preset fr3_recap] [manifest options]
levi recap import --fake --name <n>
levi recap set <n> [manifest options]
levi recap checkpoints [--verify] [--json]
levi recap inspect <n>                      strict key check (worker, CPU)
levi recap base import <folder> [--name] [--official repo] [--sha256-file f] [--weights] [--label TEXT]
levi recap base list
levi recap threshold <repo_id> <repo_id> … [--positive-quantile q] [--set <checkpoint> --provenance-text TEXT]
levi recap run <repo_id> --checkpoint <n> [--episodes 0,3] [--threshold X] [--static-filter auto|on|off]
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


def _provenance(text: str) -> dict:
    """``key=text`` (repeatable)."""
    key, sep, value = text.partition("=")
    if not sep or not key.strip():
        raise argparse.ArgumentTypeError("provenance is key=text")
    return {key.strip(): value.strip()}


def _manifest_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("manifest fields")
    group.add_argument("--env-type", dest="env_type", help="RLinf robot/env type")
    group.add_argument(
        "--model-type", dest="model_type", choices=["pi0", "pi05", "pi0_fast"]
    )
    group.add_argument("--action-dim", dest="action_dim", type=int)
    group.add_argument("--action-horizon", dest="action_horizon", type=int)
    group.add_argument(
        "--provenance",
        type=_provenance,
        action="append",
        help="key=text: where a number came from (repeatable)",
    )
    group.add_argument(
        "--no-static-filter",
        dest="no_static_filter",
        action="store_true",
        help="the training data was not static-filtered (clears static_filter)",
    )
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
        "env_type",
        "model_type",
        "action_dim",
        "action_horizon",
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
    if getattr(args, "no_static_filter", False):
        fields["static_filter"] = None
    if getattr(args, "provenance", None):
        merged: dict = {}
        for item in args.provenance:
            merged.update(item)
        fields["provenance"] = merged
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
    imp.add_argument(
        "--preset",
        help="fill the run's fields from a known RLinf setup (fr3_recap)",
    )
    _manifest_options(imp)
    edit = sub.add_parser("set", help="edit a checkpoint's manifest fields")
    edit.add_argument("name")
    _manifest_options(edit)
    listing = sub.add_parser("checkpoints", help="list checkpoints and readiness")
    listing.add_argument("--verify", action="store_true", help="re-hash the weights")
    inspect = sub.add_parser(
        "inspect", help="build the model and load the weights strictly (CPU)"
    )
    inspect.add_argument("name")
    base = sub.add_parser("base", help="shared base-model folders (_base/<name>)")
    base_sub = base.add_subparsers(dest="base_command", required=True)
    base_imp = base_sub.add_parser("import", help="copy and verify a model folder")
    base_imp.add_argument("source", type=Path)
    base_imp.add_argument("--name")
    base_imp.add_argument(
        "--official", help="Hub release to verify against (default: from the name)"
    )
    base_imp.add_argument(
        "--sha256-file", dest="sha256_file", type=Path, help="sha256sum list"
    )
    base_imp.add_argument(
        "--weights", action="store_true", help="also copy the weight files"
    )
    base_imp.add_argument(
        "--label", help="mark as development-only (e.g. an unofficial mirror)"
    )
    base_sub.add_parser("list", help="the base-model folders and their checks")
    thr = sub.add_parser(
        "threshold", help="RLinf's unified threshold over several datasets"
    )
    thr.add_argument("repo_ids", nargs="+")
    thr.add_argument("--positive-quantile", dest="positive_quantile", type=float)
    thr.add_argument("--set", dest="set_checkpoint", help="store it on a checkpoint")
    thr.add_argument(
        "--provenance-text",
        dest="provenance_text",
        help="where the threshold comes from (stored with --set)",
    )
    run = sub.add_parser("run", help="compute values and advantage labels")
    run.add_argument("repo_id")
    run.add_argument("--checkpoint", required=True)
    run.add_argument("--episodes", help="comma-separated episode indices")
    run.add_argument("--lookahead", type=int)
    run.add_argument("--positive-quantile", type=float)
    run.add_argument("--threshold", type=float)
    run.add_argument("--sft", action="store_true", help="demonstrations: all success")
    run.add_argument(
        "--static-filter",
        choices=["auto", "on", "off"],
        default="auto",
        help="the training data's static-pose filter (auto: raw-capture views "
        "when the checkpoint names one)",
    )
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
                    preset=args.preset,
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
        if args.command == "inspect":
            report = checkpoints.strict_check(args.name)
            _print(report)
            return 0 if report.get("ok") else 1
        if args.command == "base":
            from . import base_models

            if args.base_command == "list":
                _print(base_models.listing(checkpoints.store_dir()))
                return 0
            _print(
                base_models.import_base(
                    checkpoints.store_dir(),
                    args.source,
                    args.name,
                    official=args.official,
                    sha256_file=args.sha256_file,
                    weights=args.weights,
                    label=args.label,
                )
            )
            return 0
        if args.command == "threshold":
            from . import jobs

            found = jobs.unified_threshold(args.repo_ids, args.positive_quantile)
            if args.set_checkpoint:
                text = args.provenance_text or (
                    "recomputed by LEVI over " + " + ".join(args.repo_ids)
                )
                checkpoints.update(
                    args.set_checkpoint,
                    {
                        "unified_threshold": found["threshold"],
                        "provenance": {"unified_threshold": text},
                    },
                )
                found["stored_on"] = args.set_checkpoint
            _print(found)
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
        static_filter=args.static_filter,
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
