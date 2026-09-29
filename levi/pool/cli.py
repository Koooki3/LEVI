"""``levi pool``: scan the pool roots, browse the index, save recipes, export."""

import argparse
import json
import sys


def _print(value) -> None:
    print(json.dumps(value, indent=1, ensure_ascii=False, default=str))


def _policy_flags(parser) -> None:
    parser.add_argument("--policy-model", action="append", help="e.g. pi05_fr3_all")
    parser.add_argument(
        "--policy-checkpoint", action="append", help="e.g. pi05_fr3_all_step49999"
    )
    parser.add_argument(
        "--policy-method",
        action="append",
        choices=["direct", "dsrl", "rlt", "sfe", "student", "other", "unknown"],
        help="how the rollout was run (direct = direct deployment)",
    )


def _filters(parser) -> None:
    parser.add_argument("--category", action="append", help="repeatable")
    parser.add_argument("--source", action="append", help="source id or path")
    parser.add_argument("--task", action="append", help="exact normalized task")
    parser.add_argument("--search", help="substring of the task")
    parser.add_argument("--format", action="append", dest="formats")
    parser.add_argument("--outcome", choices=["success", "failure"])
    parser.add_argument("--policy", action="append", help="checkpoint name (old)")
    _policy_flags(parser)
    parser.add_argument("--show-heldout", action="store_true")
    parser.add_argument("--show-copies", action="store_true")
    parser.add_argument("--show-archive", action="store_true")


def _filter_args(args) -> dict:
    return {
        "categories": args.category,
        "sources": args.source,
        "tasks": args.task,
        "search": args.search,
        "formats": args.formats,
        "outcome": args.outcome,
        "policies": args.policy,
        "policy_models": args.policy_model,
        "policy_checkpoints": args.policy_checkpoint,
        "policy_methods": args.policy_method,
        "show_heldout": args.show_heldout,
        "show_copies": args.show_copies,
        "show_archive": args.show_archive,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="levi pool",
        description="Training pool: every dataset under LEVI_POOL_ROOTS "
        "(read-only), indexed per episode, composed and exported",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    scan = sub.add_parser("scan", help="walk the pool roots and refresh the index")
    scan.add_argument(
        "--rehash", action="store_true", help="recompute every fingerprint"
    )
    sub.add_parser("status", help="the last scan's summary")
    sources = sub.add_parser("sources", help="datasets found under the roots")
    sources.add_argument("--category")
    sources.add_argument("--show-archive", action="store_true")
    tasks = sub.add_parser("tasks", help="episodes per normalized task")
    _filters(tasks)
    episodes = sub.add_parser("episodes", help="the episode index")
    _filters(episodes)
    episodes.add_argument("--limit", type=int, default=50)
    episodes.add_argument("--offset", type=int, default=0)
    recipe = sub.add_parser("recipe", help="saved selections")
    rsub = recipe.add_subparsers(dest="recipe_action", required=True)
    save = rsub.add_parser("save", help="save a recipe (JSON file or flags)")
    save.add_argument("name")
    save.add_argument("--file", help="a JSON recipe; flags below override it")
    save.add_argument("--category", action="append")
    save.add_argument("--source", action="append")
    save.add_argument(
        "--task",
        action="append",
        help="repeatable; the export follows this order. `text` or "
        "`text:count=50,success=0.6,strategy=quality` (count: N or all; "
        "success: 0..1, 60% or natural; strategy: quality, random, first)",
    )
    save.add_argument(
        "--outcome",
        choices=[
            "all",
            "robot_flag_success",
            "verified_success",
            "human_verified_success",
        ],
    )
    save.add_argument("--per-task-cap", type=int)
    save.add_argument("--seed", type=int)
    save.add_argument("--policy", action="append", help="checkpoint name (old)")
    _policy_flags(save)
    save.add_argument("--date-from")
    save.add_argument("--date-to")
    save.add_argument("--include-nonstandard", action="store_true")
    save.add_argument(
        "--allow-unlinked-sources",
        action="store_true",
        help="allow a task from raw captures and an unlinked LeRobot dataset",
    )
    save.add_argument("--format", action="append", dest="formats", help="repeatable")
    save.add_argument(
        "--exclude", action="append", help="episode key to leave out (repeatable)"
    )
    save.add_argument(
        "--task-text",
        action="append",
        default=[],
        help="normalized task=text written into the export (repeatable)",
    )
    show = rsub.add_parser("show", help="a recipe and its preview")
    show.add_argument("name")
    show.add_argument("--format", choices=["lerobot_v21", "recap_value", "raw_capture"])
    show.add_argument(
        "--human-as-success",
        action="store_true",
        help="recap_value: count human demonstrations without an outcome as success",
    )
    picked = rsub.add_parser("episodes", help="the episodes a recipe picks for a task")
    picked.add_argument("name")
    picked.add_argument("--task", required=True)
    picked.add_argument(
        "--format", choices=["lerobot_v21", "recap_value", "raw_capture"]
    )
    suggest = rsub.add_parser(
        "suggest", help="availability and a balanced default count for a new task"
    )
    suggest.add_argument("name")
    suggest.add_argument("--task", required=True)
    rsub.add_parser("list", help="saved recipes")
    delete = rsub.add_parser("delete", help="delete a saved recipe")
    delete.add_argument("name")
    export = sub.add_parser("export", help="export a saved recipe")
    export.add_argument("recipe")
    export.add_argument(
        "--format", required=True, choices=["lerobot_v21", "recap_value", "raw_capture"]
    )
    export.add_argument("--name", required=True, help="the new dataset's folder name")
    export.add_argument("--output-dir", help="parent folder (in LEVI_EXPORT_ROOTS)")
    export.add_argument("--fps", type=float, default=10)
    export.add_argument(
        "--camera-map",
        action="append",
        default=[],
        help="LeRobot source key=output key (repeatable)",
    )
    export.add_argument(
        "--hardlink",
        action="store_true",
        help="raw_capture: hard-link videos (small files are copied)",
    )
    export.add_argument(
        "--human-as-success",
        action="store_true",
        help="recap_value: count human demonstrations without an outcome as success",
    )
    export.add_argument(
        "--camera",
        action="append",
        default=[],
        help="raw capture camera=output key (repeatable)",
    )
    export.add_argument("--timing", choices=["resample", "retime"])
    export.add_argument(
        "--filter-static",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="drop static frames of raw captures (default: lerobot yes, recap no)",
    )
    export.add_argument("--failure-reward", type=float)
    export.add_argument("--dry-run", action="store_true", help="plan only")
    remotes = sub.add_parser("remote", help="remote targets for push (SSH keys only)")
    msub = remotes.add_subparsers(dest="remote_action", required=True)
    add = msub.add_parser("add", help="register or replace a target")
    add.add_argument("name")
    add.add_argument("spec", help="[user@]host:/path (host may be an ssh alias)")
    add.add_argument("--port", type=int)
    add.add_argument("--description", default="")
    msub.add_parser("list", help="registered targets")
    forget = msub.add_parser("delete", help="forget a target")
    forget.add_argument("name")
    push = sub.add_parser(
        "push", help="send a finished export to a remote target (rsync over SSH)"
    )
    push.add_argument("export_dir")
    push.add_argument("--target", required=True, help="a registered target name")
    push.add_argument(
        "--dry-run", action="store_true", help="list what would be sent (rsync -n)"
    )
    return parser


def main(argv=None) -> int:
    from ..paths import configure

    configure()
    from . import index, jobs, recipe, remote, settings

    args = build_parser().parse_args(argv)
    try:
        if args.action == "scan":
            settings.require_enabled()
            from .scanner import scan

            _print(scan(rehash=args.rehash))
        elif args.action == "status":
            _print(
                {
                    "enabled": settings.enabled(),
                    "roots": [str(p) for p in settings.pool_roots()],
                    "export_roots": [str(p) for p in settings.export_roots()],
                    "heldout_lists": [str(p) for p in settings.heldout_files()],
                    "last_scan": index.summary() or None,
                }
            )
        elif args.action == "sources":
            _print(index.sources(args.category, args.show_archive))
        elif args.action == "tasks":
            _print(index.tasks(**_filter_args(args)))
        elif args.action == "episodes":
            _print(
                index.episodes(
                    limit=args.limit, offset=args.offset, **_filter_args(args)
                )
            )
        elif args.action == "recipe":
            if args.recipe_action == "save":
                value = {}
                if args.file:
                    with open(args.file) as handle:
                        value = json.load(handle)
                value["name"] = args.name
                for key, given in (
                    ("categories", args.category),
                    ("sources", args.source),
                    ("outcome", args.outcome),
                    ("per_task_cap", args.per_task_cap),
                    ("seed", args.seed),
                    ("policies", args.policy),
                    ("policy_models", args.policy_model),
                    ("policy_checkpoints", args.policy_checkpoint),
                    ("policy_methods", args.policy_method),
                    ("date_from", args.date_from),
                    ("date_to", args.date_to),
                    ("formats", args.formats),
                    ("exclude", args.exclude),
                ):
                    if given is not None:
                        value[key] = given
                if args.task is not None:
                    value["tasks"] = [recipe.parse_task_spec(t) for t in args.task]
                if args.task_text:
                    value["task_text"] = dict(t.split("=", 1) for t in args.task_text)
                if args.include_nonstandard:
                    value["include_nonstandard"] = True
                if args.allow_unlinked_sources:
                    value["allow_unlinked_sources"] = True
                _print(recipe.save(recipe.Recipe.model_validate(value)))
            elif args.recipe_action == "show":
                _print(
                    recipe.preview(
                        recipe.load(args.name),
                        args.format,
                        human_as_success=args.human_as_success,
                    )
                )
            elif args.recipe_action == "episodes":
                from .rules import normalize_task

                _print(
                    recipe.selected_episodes(
                        recipe.load(args.name), normalize_task(args.task), args.format
                    )
                )
            elif args.recipe_action == "suggest":
                _print(recipe.suggest(recipe.load(args.name), args.task))
            elif args.recipe_action == "list":
                _print(recipe.listing())
            else:
                _print({"deleted": recipe.delete(args.name)})
        elif args.action == "export":
            from .export import ExportOptions

            extra = {}
            if args.camera:
                extra["cameras"] = dict(p.split("=", 1) for p in args.camera)
            if args.failure_reward is not None:
                extra["failure_reward"] = args.failure_reward
            options = ExportOptions(
                format=args.format,
                name=args.name,
                output_dir=args.output_dir,
                fps=args.fps,
                camera_map=dict(p.split("=", 1) for p in args.camera_map),
                hardlink=args.hardlink,
                human_as_success=args.human_as_success,
                timing=args.timing,
                filter_static=args.filter_static,
                **extra,
            )
            job = jobs.plan_export(recipe.load(args.recipe), options)
            brief = jobs._brief(job)
            if args.dry_run:
                jobs.discard(job["id"])
                _print(brief)
                return 0
            print(
                f"exporting {brief['planned_episodes']} episodes to {job['target']}",
                file=sys.stderr,
            )
            from ..catalog import atomic

            try:
                result = jobs.execute(job)
            except (Exception, KeyboardInterrupt) as exc:
                job.update(status="failed", error=f"{type(exc).__name__}: {exc}")
                atomic(jobs._path(job["id"]), job)
                raise
            job.update(status="succeeded", result=result)
            atomic(jobs._path(job["id"]), job)
            _print(result)
        elif args.action == "remote":
            if args.remote_action == "add":
                _print(
                    remote.save(
                        remote.target_from(
                            args.name,
                            {
                                "spec": args.spec,
                                "port": args.port,
                                "description": args.description,
                            },
                        )
                    )
                )
            elif args.remote_action == "list":
                _print(remote.listing())
            else:
                _print({"deleted": remote.delete(args.name)})
        elif args.action == "push":
            target = remote.get(args.target)

            def echo(update):
                print(
                    f"\r{update['percent']:3d}%  {update['bytes']:,} B  "
                    f"{update['rate']}  eta {update['eta_seconds']}s",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )

            result = remote.push(args.export_dir, target, args.dry_run, echo=echo)
            print(file=sys.stderr)
            _print(result)
            return 0 if result["ok"] else 1
    except KeyError as exc:
        print(f"ERROR: not found: {exc}")
        return 1
    except (ValueError, PermissionError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0
