"""``levi pool``: scan the pool roots, browse the index, save recipes, export."""

import argparse
import json
import sys


def _print(value) -> None:
    print(json.dumps(value, indent=1, ensure_ascii=False, default=str))


def _filters(parser) -> None:
    parser.add_argument("--category", action="append", help="repeatable")
    parser.add_argument("--source", action="append", help="source id or path")
    parser.add_argument("--task", action="append", help="exact normalized task")
    parser.add_argument("--search", help="substring of the task")
    parser.add_argument("--format", action="append", dest="formats")
    parser.add_argument("--outcome", choices=["success", "failure"])
    parser.add_argument("--policy", action="append")
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
        "--task", action="append", help="repeatable; the export follows this order"
    )
    save.add_argument(
        "--outcome", choices=["all", "robot_flag_success", "verified_success"]
    )
    save.add_argument("--per-task-cap", type=int)
    save.add_argument("--seed", type=int)
    save.add_argument("--policy", action="append")
    save.add_argument("--date-from")
    save.add_argument("--date-to")
    save.add_argument("--include-nonstandard", action="store_true")
    show = rsub.add_parser("show", help="a recipe and its preview")
    show.add_argument("name")
    show.add_argument(
        "--format", choices=["lerobot_v21", "recap_value", "raw_capture"]
    )
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
    export.add_argument("--hardlink", action="store_true")
    export.add_argument("--dry-run", action="store_true", help="plan only")
    return parser


def main(argv=None) -> int:
    from ..paths import configure

    configure()
    from . import index, jobs, recipe, settings

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
                    ("tasks", args.task),
                    ("outcome", args.outcome),
                    ("per_task_cap", args.per_task_cap),
                    ("seed", args.seed),
                    ("policies", args.policy),
                    ("date_from", args.date_from),
                    ("date_to", args.date_to),
                ):
                    if given is not None:
                        value[key] = given
                if args.include_nonstandard:
                    value["include_nonstandard"] = True
                _print(recipe.save(recipe.Recipe.model_validate(value)))
            elif args.recipe_action == "show":
                _print(recipe.preview(recipe.load(args.name), args.format))
            elif args.recipe_action == "list":
                _print(recipe.listing())
            else:
                _print({"deleted": recipe.delete(args.name)})
        elif args.action == "export":
            from .export import ExportOptions

            options = ExportOptions(
                format=args.format,
                name=args.name,
                output_dir=args.output_dir,
                fps=args.fps,
                camera_map=dict(p.split("=", 1) for p in args.camera_map),
                hardlink=args.hardlink,
            )
            job = jobs.plan_export(recipe.load(args.recipe), options)
            brief = jobs._brief(job)
            if args.dry_run:
                _print(brief)
                return 0
            print(
                f"exporting {brief['planned_episodes']} episodes to {job['target']}",
                file=sys.stderr,
            )
            result = jobs.execute(job)
            job.update(status="succeeded", result=result)
            from ..catalog import atomic

            atomic(jobs._path(job["id"]), job)
            _print(result)
    except KeyError as exc:
        print(f"ERROR: not found: {exc}")
        return 1
    except (ValueError, PermissionError) as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0
