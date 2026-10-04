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


def _embodiment_flags(parser) -> None:
    parser.add_argument(
        "--robot", action="append", help="e.g. franka_fr3 (unknown: none recorded)"
    )
    parser.add_argument(
        "--gripper",
        action="append",
        help="e.g. robotiq_2f85, franka_hand (unknown: none recorded); repeatable",
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
    _embodiment_flags(parser)
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
        "robots": args.robot,
        "grippers": args.gripper,
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
    _embodiment_flags(save)
    save.add_argument(
        "--allow-mixed-gripper",
        action="store_true",
        help="let one export hold more than one gripper (refused otherwise)",
    )
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
    show.add_argument(
        "--fps",
        type=float,
        help="export fps: also note how raw captures measured below or above it fare",
    )
    show.add_argument(
        "--timing",
        choices=["resample", "retime"],
        help="with --fps: timing mode to judge (default: the format's own)",
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
    export = sub.add_parser(
        "export",
        help="export a saved recipe (or --resume an interrupted export)",
        description="Export a recipe into a new dataset. An export that was "
        "interrupted (the service stopped, the process was killed) keeps its "
        ".partial folder and a journal: `levi pool export --resume <job-id>` "
        "continues from the finished units (`levi pool jobs` lists the ids).",
    )
    export.add_argument("recipe", nargs="?", help="saved recipe name")
    export.add_argument(
        "--resume",
        metavar="JOB",
        help="continue this interrupted or failed export job from its journal",
    )
    export.add_argument(
        "--format", choices=["lerobot_v21", "recap_value", "raw_capture"]
    )
    export.add_argument("--name", help="the new dataset's folder name")
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
    export.add_argument(
        "--timing",
        choices=["resample", "retime"],
        help="lerobot_v21 / recap_value: resample drops frames only (default of "
        "lerobot_v21; refused when a raw source runs below --fps), retime keeps "
        "every frame and declares it at --fps (default of recap_value); "
        "ignored for raw_capture",
    )
    export.add_argument(
        "--filter-static",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="drop static frames of raw captures (default: lerobot yes, recap no)",
    )
    export.add_argument("--failure-reward", type=float)
    export.add_argument(
        "--on-error-max-fraction",
        type=float,
        help="stop (resumable) when more than this share of the episodes failed "
        "to convert; failing episodes are left out and listed (default 0.1)",
    )
    export.add_argument("--dry-run", action="store_true", help="plan only")
    listing = sub.add_parser(
        "jobs",
        help="scan, export and push jobs: list, log, delete, clear-failed",
        description="`levi pool jobs` lists jobs with their states; `log ID` prints "
        "a job's log; `delete ID [--files]` clears a finished job's record (and "
        "with --files the export it produced); `clear-failed` removes the records "
        "and leftovers of failed, interrupted and cancelled jobs.",
    )
    listing.add_argument("--limit", type=int, default=20)
    jsub = listing.add_subparsers(dest="jobs_action")
    jlog = jsub.add_parser("log", help="the tail of a job's log")
    jlog.add_argument("job")
    jdel = jsub.add_parser(
        "delete",
        help="clear a job's record; --files also deletes the export it produced",
    )
    jdel.add_argument("job")
    jdel.add_argument(
        "--files",
        action="store_true",
        help="also delete the export directory (and leftover partial) of this job",
    )
    jdel.add_argument("--yes", action="store_true", help="do not ask")
    jdel.add_argument(
        "--force",
        action="store_true",
        help="delete even if the folder was changed since the export",
    )
    jclear = jsub.add_parser(
        "clear-failed",
        help="remove failed, interrupted and cancelled jobs and their leftovers",
    )
    jclear.add_argument("--yes", action="store_true", help="do not ask")
    clean = sub.add_parser(
        "clean",
        help="remove what jobs left behind: expired partial exports, old job files",
        description="Sweeps `.partial` folders of finished or cancelled exports, "
        "interrupted or failed ones older than LEVI_POOL_PARTIAL_TTL (default 3 "
        "days), job logs older than LEVI_POOL_JOB_TTL (default 30 days) and stale "
        "temporaries. A running job's files and finished exports are never touched.",
    )
    clean.add_argument("--dry-run", action="store_true", help="list, remove nothing")
    clean.add_argument(
        "--all-partials",
        action="store_true",
        help="also remove interrupted/failed partials that could still be resumed",
    )
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
    live = sub.add_parser(
        "live-workspaces",
        help="live workspaces whose removed episodes this pool keeps out",
    )
    lsub = live.add_subparsers(dest="live_action", required=True)
    lsub.add_parser("list", help="the remembered ones (and the one shown now)")
    lforget = lsub.add_parser(
        "forget", help="stop reading one (deletes nothing; listed again if shown again)"
    )
    lforget.add_argument("path")
    corr = sub.add_parser(
        "corrections",
        help="task text corrections: import proposals, list, show; "
        "approve or reject (a person's action, through the running service)",
    )
    csub = corr.add_subparsers(dest="corrections_action", required=True)
    cimp = csub.add_parser("import", help="import proposals (JSONL or a JSON list)")
    cimp.add_argument("file")
    cimp.add_argument("--version", required=True, help="e.g. cast-direction-v1")
    csub.add_parser("list", help="versions and counts per status")
    cshow = csub.add_parser("show", help="one version's proposals and status")
    cshow.add_argument("version")
    cshow.add_argument("--status", choices=["proposed", "approved", "rejected"])
    cshow.add_argument("--batch", help="only this review batch")
    csub.add_parser(
        "copies", help="copies whose task texts differ (for a person to decide)"
    )
    for verb in ("approve", "reject"):
        cv = csub.add_parser(
            verb,
            help=f"{verb} proposals: a person's action, sent to the running "
            "LEVI service with the person's key",
        )
        cv.add_argument("version")
        pick = cv.add_mutually_exclusive_group(required=True)
        pick.add_argument("--id", action="append", dest="ids", help="repeatable")
        pick.add_argument("--batch", help="every proposal of this review batch")
        pick.add_argument("--all", action="store_true", help="every proposal")
        cv.add_argument(
            "--except",
            action="append",
            dest="exclude",
            default=[],
            help="leave this id out (repeatable)",
        )
        cv.add_argument("--reviewer", required=True, help="who decides")
        cv.add_argument(
            "--sha256",
            required=True,
            help="the version's sha256 as `show` / `list` print it: the "
            "decision binds to the proposals you looked at",
        )
        cv.add_argument("--note", default="")
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
                    ("robots", args.robot),
                    ("grippers", args.gripper),
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
                if args.allow_mixed_gripper:
                    value["allow_mixed_gripper"] = True
                _print(recipe.save(recipe.Recipe.model_validate(value)))
            elif args.recipe_action == "show":
                _print(
                    recipe.preview(
                        recipe.load(args.name),
                        args.format,
                        human_as_success=args.human_as_success,
                        fps=args.fps,
                        timing=args.timing,
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
            return _export(args, jobs, recipe)
        elif args.action == "jobs":
            if args.jobs_action == "log":
                print(jobs.log_tail(args.job, 128))
                return 0
            if args.jobs_action == "delete":
                return _delete_job(args)
            if args.jobs_action == "clear-failed":
                return _clear_failed(args)
            _print(
                [
                    {
                        k: j.get(k)
                        for k in (
                            "id",
                            "kind",
                            "status",
                            "age_seconds",
                            "resumable",
                            "error",
                        )
                    }
                    for j in jobs.listing(args.limit)
                ]
            )
        elif args.action == "clean":
            from . import cleanup

            _print(cleanup.sweep(dry_run=args.dry_run, all_partials=args.all_partials))
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
        elif args.action == "live-workspaces":
            from . import exclusions

            if args.live_action == "forget":
                if not exclusions.forget(args.path):
                    print(f"not remembered: {args.path}", file=sys.stderr)
                    return 1
                print(f"forgotten (nothing deleted): {args.path}")
            else:
                _print(exclusions.listing())
        elif args.action == "corrections":
            return _corrections(args)
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


def _export(args, jobs, recipe) -> int:
    """``levi pool export``: plan and run in this process (with the job
    worker's record keeping, heartbeat and journal), or ``--resume`` one."""
    from .export import ExportOptions

    if args.resume:
        job = jobs.prepare_resume(args.resume)
        print(f"resuming {job['id']} -> {job['target']}", file=sys.stderr)
    else:
        if not (args.recipe and args.format and args.name):
            raise ValueError("export needs a recipe, --format and --name (or --resume)")
        extra = {}
        if args.camera:
            extra["cameras"] = dict(p.split("=", 1) for p in args.camera)
        if args.failure_reward is not None:
            extra["failure_reward"] = args.failure_reward
        if args.on_error_max_fraction is not None:
            extra["on_error_max_fraction"] = args.on_error_max_fraction
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
            f"exporting {brief['planned_episodes']} episodes to {job['target']} "
            f"(job {job['id']}; if it is interrupted: levi pool export --resume "
            f"{job['id']})",
            file=sys.stderr,
        )
    code = jobs.run_worker(jobs._path(job["id"]))
    final = jobs.get(job["id"])
    if final["status"] in ("done", "done_with_errors"):
        _print(final["result"])
        if final["status"] == "done_with_errors":
            print(
                f"finished with {final['result'].get('errors')} episode(s) left out: "
                f"levi pool jobs --log {job['id']}",
                file=sys.stderr,
            )
        return 0
    info = final.get("error_info") or {}
    print(f"ERROR ({final['status']}): {final.get('error')}")
    if info.get("hint"):
        print(f"  {info['hint']}")
    if final.get("resumable"):
        print(f"  resume with: levi pool export --resume {job['id']}")
    return code or 1


def _corrections(args) -> int:
    """``levi pool corrections``. Import, list, show and copies read and
    write the pool's own files; approve and reject are a person's decision
    and go to the running service as the person (its review route refuses
    an agent's credential)."""
    from . import corrections

    action = args.corrections_action
    if action == "import":
        _print(corrections.import_file(args.file, args.version))
    elif action == "list":
        _print(corrections.listing())
    elif action == "show":
        _print(corrections.show(args.version, args.status, args.batch))
    elif action == "copies":
        _print(corrections.copy_candidates())
    else:
        from urllib.parse import quote

        from ..agent import core

        corrections.check_version(args.version)
        decision = corrections.Review(
            decision="approved" if action == "approve" else "rejected",
            reviewer=args.reviewer,
            sha256=args.sha256,
            ids=args.ids or [],
            batch=args.batch,
            all=args.all,
            exclude=args.exclude,
            note=args.note,
        )
        if not core.status():
            raise ValueError(
                "The LEVI service is not running: a review is a person's action "
                "and is recorded by the running service (start it with "
                "`levi serve`); the page has no review controls"
            )
        _print(
            core.request(
                f"/api/levi/pool/corrections/{quote(args.version, safe='')}/review",
                decision.model_dump(),
                human=True,
            )
        )
    return 0


def _confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        print(f"{question}\nNot confirmed: pass --yes to go ahead.", file=sys.stderr)
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _gib(n) -> str:
    return f"{(n or 0) / 1024**3:.2f} GiB"


def _delete_job(args) -> int:
    from . import deletion

    wanted = deletion.plan(args.job, args.files)
    if wanted["refused"]:
        print(f"ERROR: {wanted['refused']}")
        return 1
    print(f"job {wanted['id']} ({wanted['kind']}, {wanted['status']})")
    print(f"  record: {wanted['record_files']} file(s) will be cleared")
    for item in wanted["outputs"]:
        verdict = (
            "will be deleted"
            if item["will_delete"]
            else (item.get("kept_because") or "stays")
        )
        print(
            f"  {item['role']}: {item['path']} ({_gib(item.get('bytes'))}, "
            f"{item.get('episodes')} episodes, {item.get('format')}, "
            f"created {item.get('created_at')}) - {verdict}"
        )
        for push in item.get("pushed") or []:
            print(f"    pushed to {push['remote']}: {push['status']}")
        for note in item["needs_force"]:
            print(f"    WARNING: {note}")
    if wanted["needs_force"] and not args.force:
        print(
            "ERROR: the folder was changed since the export; pass --force to delete it"
        )
        return 1
    if not _confirm("Go ahead?", args.yes):
        return 1
    _print(deletion.delete(args.job, args.files, args.force, how="cli"))
    return 0


def _clear_failed(args) -> int:
    from . import deletion, jobs

    stopped = [j for j in jobs.listing(500) if j["status"] in deletion.STOPPED_STATES]
    if not stopped:
        print("No failed, interrupted or cancelled jobs.")
        return 0
    for j in stopped:
        print(f"  {j['id']}  {j['status']}  {j.get('error') or ''}"[:160])
    if not _confirm(
        f"Remove these {len(stopped)} job(s) and their partial folders? "
        "(finished exports are not touched)",
        args.yes,
    ):
        return 1
    _print(deletion.clear_failed(how="cli clear-failed"))
    return 0
