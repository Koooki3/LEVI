"""Explicit human CLI plus project-scoped MCP setup. No model calls on setup."""

import argparse
import difflib
import json
import subprocess
import sys
import tomllib
from pathlib import Path

from . import core

BASE = "/api/levi/agent/v1/"


def api(path, value=None):
    core.ensure()
    return core.request(BASE + path, value, human=True)


def tool(name, args):
    return api(
        "tools",
        {
            "name": name,
            "arguments": args,
            "idempotency_key": f"cli:{args.get('changeset_id')}:{args.get('revision')}"
            if name == "changes.commit"
            else None,
        },
    )


def call(args):
    """One capability as the connected agent (LEVI_AGENT_GRANT_FILE or
    LEVI_AGENT_TOKEN): the JSON answer on one line, then one ``IMAGE: <path>``
    line per picture it carries, saved as files the agent can open."""
    from levi.paths import ROOT

    from .mcp import images
    from .mcp import request as agent_request

    text = args.arguments
    if text == "@-":
        text = sys.stdin.read()
    elif text.startswith("@"):
        text = Path(text[1:]).read_text()
    arguments = json.loads(text or "{}")
    value = agent_request("tools", {"name": args.capability, "arguments": arguments})
    print(json.dumps(value, ensure_ascii=False))
    paths = images(args.capability, arguments, value)
    if paths:
        folder = args.images or (
            ROOT / "tmp" / "agent-images" / str(arguments.get("run_id", "run"))
        )
        folder.mkdir(parents=True, exist_ok=True)
        for path in paths:
            target = folder / Path(path).name.replace("%2F", "_")
            target.write_bytes(agent_request(path, binary=True))
            print(f"IMAGE: {target}")
    return 0


def confirm(value, question="Approve this exact revision?"):
    print(json.dumps(value, ensure_ascii=False, indent=2))
    if (
        not sys.stdin.isatty()
        or input(f"{question} Type approve: ").strip() != "approve"
    ):
        raise ValueError("Human approval required; no change applied")


def configuration(client, project, grant_file):
    """Return reviewed text, preserving unrelated project configuration."""
    command = sys.executable
    args = ["-m", "levi.agent.control", "mcp"]
    env = {
        "LEVI_WORKSPACE": str(__import__("levi.paths", fromlist=["ROOT"]).ROOT),
        "LEVI_AGENT_GRANT_FILE": str(grant_file),
    }
    if client == "claude":
        path = project / ".mcp.json"
        before = path.read_text() if path.exists() else "{}\n"
        data = json.loads(before)
        if "levi" in data.get("mcpServers", {}):
            raise ValueError(
                "A levi MCP entry already exists; disconnect/remove that entry explicitly first"
            )
        data.setdefault("mcpServers", {})["levi"] = {
            "type": "stdio",
            "command": command,
            "args": args,
            "env": env,
        }
        return path, before, json.dumps(data, indent=2) + "\n"
    path = project / ".codex" / "config.toml"
    before = path.read_text() if path.exists() else ""
    data = tomllib.loads(before)
    if "levi" in data.get("mcp_servers", {}):
        raise ValueError(
            "A levi MCP entry already exists; remove that entry explicitly first"
        )
    after = (
        before
        + "\n[mcp_servers.levi]\ncommand = "
        + json.dumps(command)
        + "\nargs = "
        + json.dumps(args)
        + "\n[mcp_servers.levi.env]\n"
    )
    after += "".join(f"{key} = {json.dumps(value)}\n" for key, value in env.items())
    tomllib.loads(after)
    return path, before, after


def connect(args):
    if not args.dataset:
        raise ValueError("Supply --dataset catalog/id (repeatable)")
    from levi.paths import STATE

    from .runtime import new_id

    # Readable: which client, and since when; a second connection in the
    # same minute gets -2, -3... like every other id. The grant id is
    # recorded in connection.json once the service has issued it.
    folder = STATE / "agent" / "connections"
    prefix = f"{args.client}-"
    taken = {p.name[len(prefix) :] for p in folder.glob(prefix + "*") if p.is_dir()}
    identifier = new_id(taken)
    credential = folder / f"{args.client}-{identifier}" / "credential"
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        raise ValueError("Project directory must exist")
    path, before, after = configuration(args.client, project, credential)
    print(
        "".join(
            difflib.unified_diff(
                before.splitlines(True),
                after.splitlines(True),
                fromfile=str(path),
                tofile=str(path),
            )
        )
    )
    print("Scope: " + ", ".join(args.dataset))
    if not args.apply:
        print("Review, then repeat with --apply. No configuration written.")
        return
    result = api(
        "grants",
        {
            "client": args.client,
            "datasets": args.dataset,
            "hours": args.hours,
            "max_tool_calls": args.max_calls,
        },
    )
    credential.parent.mkdir(parents=True, mode=0o700)
    credential.write_text(result["token"])
    credential.chmod(0o600)
    try:
        if (
            path.read_text()
            if path.exists()
            else ("{}\n" if args.client == "claude" else "")
        ) != before:
            raise ValueError("Configuration changed while connecting; retry")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(after)
        # Remembered so disconnect can take back exactly the entry it wrote.
        (credential.parent / "connection.json").write_text(
            json.dumps(
                {
                    "client": args.client,
                    "grant_id": result["id"],
                    "project": str(project),
                    "config": str(path),
                }
            )
        )
        skill = Path(__file__).parent / "skills" / "levi-overview" / "SKILL.md"
        skill_dir = (
            project
            / (".claude" if args.client == "claude" else ".agents")
            / "skills"
            / "levi-overview"
        )
        if not skill_dir.exists():
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(skill.read_text())
    except Exception:
        api(f"grants/{result['id']}/revoke", {})
        raise
    print(json.dumps({k: v for k, v in result.items() if k != "token"}, indent=2))
    print(
        "Reload MCP in your client. Credentials are not stored in project configuration."
    )


def digest_change(change):
    """What a reviewer decides on, without the whole ChangeSet: per episode,
    the segments with time, subtask and outcome; plus revision and base.
    `levi agent changes show <id>` prints everything."""
    episodes = {}
    for index, proposal in enumerate(change["proposals"]):
        decision = change.get("decisions", {}).get(str(index), "pending")
        episodes.setdefault(f"episode_{proposal['episode_index']:06d}", []).append(
            f"{proposal.get('start', 0):.1f}-{proposal.get('end') or 0:.1f}s "
            f"{proposal.get('subtask_id') or proposal['kind']} "
            f"{proposal.get('outcome') or ''} [{decision}]".strip()
        )
    return {
        "changeset": change["id"],
        "run": change["run_id"],
        "revision": change["revision"],
        "status": change["status"],
        "base_revision": change["base_revision"],
        "proposals": len(change["proposals"]),
        "episodes": episodes,
        "full": f"levi agent changes show {change['id']}",
    }


def forget_configuration(identifier):
    """Remove the project MCP entry this connection wrote, and nothing else.

    Only an entry whose grant file is this connection's credential is taken
    out; one someone edited to point elsewhere is left and reported. Without
    this, a revoked connection kept its entry and blocked reconnecting.
    """
    from levi.paths import STATE

    connections = STATE / "agent" / "connections"
    folder, info = None, None
    for candidate in sorted(connections.glob("*/connection.json")):
        data = json.loads(candidate.read_text())
        if data.get("grant_id") == identifier:
            folder, info = candidate.parent, data
            break
    if folder is None and (connections / identifier / "connection.json").exists():
        # Connections made before the grant id was recorded.
        folder = connections / identifier
        info = json.loads((folder / "connection.json").read_text())
    if folder is None:
        return {"configuration": "no record of a project entry for this connection"}
    path = Path(info["config"])
    credential = str(folder / "credential")
    if info["client"] != "claude":
        return {
            "configuration": f"remove [mcp_servers.levi] from {path} by hand; "
            "LEVI does not rewrite TOML"
        }
    if not path.exists():
        return {"configuration": f"{path} is already gone"}
    data = json.loads(path.read_text())
    entry = data.get("mcpServers", {}).get("levi")
    if not entry or entry.get("env", {}).get("LEVI_AGENT_GRANT_FILE") != credential:
        return {"configuration": f"{path} has no levi entry for this connection"}
    del data["mcpServers"]["levi"]
    path.write_text(json.dumps(data, indent=2) + "\n")
    return {"configuration": f"removed the levi entry from {path}"}


def build_parser():
    """The ``levi agent`` command line (also read by ``levi docs check``)."""
    p = argparse.ArgumentParser(description="LEVI local Pilot tools; no UI required")
    commands = p.add_subparsers(dest="command", required=True)
    c = commands.add_parser("core")
    c.add_argument("action", choices=["status", "start", "stop"])
    c = commands.add_parser("connect")
    c.add_argument("--client", choices=["codex", "claude"], required=True)
    c.add_argument("--project", default=".")
    c.add_argument("--dataset", action="append")
    # No limit unless one is asked for: a local connection lasts until the
    # operator disconnects it.
    c.add_argument("--hours", type=int, default=None)
    c.add_argument("--max-calls", type=int, default=None)
    c.add_argument("--apply", action="store_true")
    c = commands.add_parser("disconnect")
    c.add_argument("id")
    c = commands.add_parser(
        "call",
        help="Call one capability as the connected agent, for agents that work "
        "from a shell (what the MCP bridge does)",
    )
    c.add_argument("capability")
    c.add_argument(
        "arguments",
        nargs="?",
        default="{}",
        help="JSON arguments, @file.json, or @- to read them from stdin",
    )
    c.add_argument(
        "--images",
        type=Path,
        help="folder for the images an answer carries "
        "(default: <workspace>/tmp/agent-images/<run>)",
    )
    c = commands.add_parser("plan")
    c.add_argument("action", choices=["create", "show", "approve"])
    c.add_argument("value")
    c = commands.add_parser("changes")
    c.add_argument(
        "action",
        choices=["show", "edit", "validate", "review", "rebase", "commit", "export"],
    )
    c.add_argument("id")
    c.add_argument("--file", type=Path)
    c = commands.add_parser("permission")
    c.add_argument("action", choices=["list", "answer"])
    c.add_argument("id", nargs="?")
    c.add_argument("--option")
    c = commands.add_parser("pilot")
    c.add_argument(
        "action",
        choices=["start", "review", "message", "pause", "resume", "cancel", "status"],
    )
    c.add_argument("id", nargs="?")
    c.add_argument("--runtime", choices=["codex", "claude"])
    c.add_argument("--text")
    c.add_argument("--reject", action="store_true")
    c = commands.add_parser(
        "reset", help="Remove one dataset's agent history (runs, records, revisions)"
    )
    c.add_argument("--dataset", required=True)
    c.add_argument("--apply", action="store_true")
    c = commands.add_parser(
        "improvements",
        help="Harness improvement candidates: review, publish, retain, roll back",
    )
    c.add_argument(
        "action",
        choices=["list", "show", "publish", "reject", "retain", "rollback", "resolve"],
    )
    c.add_argument("slug", nargs="?")
    c.add_argument("--dataset", required=True, help="catalog id, e.g. local/<name>")
    c.add_argument("--note", default="")
    c = commands.add_parser(
        "task", help="Natural-language task: interpret, approve, advance, show"
    )
    c.add_argument("action", choices=["new", "show", "approve", "advance"])
    c.add_argument("value", help="the request text for new, else the task id")
    c.add_argument("--provider", default="qwen-local")
    c.add_argument(
        "--supervision", default="none", choices=["none", "shadow", "supervised"]
    )
    c.add_argument("--teacher")
    c = commands.add_parser(
        "gpu", help="GPU guardian: decision, reason, learned windows"
    )
    c.add_argument("action", choices=["status"], nargs="?", default="status")
    c = commands.add_parser("memory", help="A dataset's local memory: show or rebuild")
    c.add_argument("action", choices=["show", "search", "rebuild"])
    c.add_argument("--dataset", required=True, help="catalog id, e.g. local/<name>")
    c.add_argument("--query", default="")
    c = commands.add_parser(
        "knowledge",
        help="Built-in knowledge: list, refresh candidates, promote or reject one",
    )
    c.add_argument(
        "action",
        choices=["list", "refresh", "promote", "reject"],
        nargs="?",
        default="list",
    )
    c.add_argument("candidate", nargs="?", help="candidate-NNN")
    c.add_argument("--topic", choices=["annotation", "interpretation", "harness"])
    c.add_argument("--text", help="the entry as it should read (dataset-agnostic)")
    c = commands.add_parser(
        "eval",
        help="Write the evaluation record of a committed subtask-annotation run",
    )
    c.add_argument("run", help="run id, e.g. temporal-20260922T1507")
    c = commands.add_parser("clean", help="Remove regenerable run evidence/snapshots")
    c.add_argument(
        "--abandon",
        action="append",
        default=[],
        metavar="RUN_ID",
        help="Close a run nobody will finish so its evidence can be freed",
    )
    c.add_argument("--reason", default="")
    c.add_argument("--dataset")
    c.add_argument("--older-than-days", type=int, default=0)
    c.add_argument("--include-open-runs", action="store_true")
    c.add_argument("--apply", action="store_true")
    c = commands.add_parser("usage", help="Per-agent token history and estimates")
    c.add_argument("action", choices=["show", "estimate"], nargs="?", default="show")
    c.add_argument("--run")
    c.add_argument("--episodes", type=int, default=1)
    c.add_argument("--frames", type=int)
    c.add_argument("--workflow")
    c = commands.add_parser(
        "objects", help="Headless object annotation: strategy, SAM3 run, mask review"
    )
    c.add_argument("action", choices=["status", "run", "review", "show", "relink"])
    c.add_argument("id", nargs="?", help="run id for status/show, job id otherwise")
    c.add_argument("--dataset", help="catalog name, for relink")
    c.add_argument("--apply", action="store_true", help="publish the relink")
    c.add_argument("--accept-all", action="store_true")
    c.add_argument("--reject-all", action="store_true")
    c.add_argument("--object")
    c.add_argument("--episode", type=int)
    c.add_argument("--camera")
    for name in ["result", "open", "events"]:
        c = commands.add_parser(name)
        c.add_argument("id")
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["mcp"] or not argv:
        from .mcp import main as serve

        sys.argv = [sys.argv[0]]
        return serve()
    p = build_parser()
    args = p.parse_args(argv)
    try:
        result = None
        if args.command == "core":
            result = (
                {"status": core.status()}
                if args.action == "status"
                else core.ensure()
                if args.action == "start"
                else core.stop()
            )
        elif args.command == "connect":
            return connect(args)
        elif args.command == "call":
            return call(args)
        elif args.command == "disconnect":
            result = api(f"grants/{args.id}/revoke", {})
            result = {**(result or {}), **forget_configuration(args.id)}
        elif args.command == "plan":
            if args.action == "create":
                context = json.loads(Path(args.value).read_text())
                if context.get("provider") != "external":
                    raise ValueError(
                        "Headless plans require provider=external (Codex/Claude Pilot)"
                    )
                result = tool("runs.plan", context)
            else:
                run = tool("runs.get", {"run_id": args.value})
                result = run
                if args.action == "approve":
                    confirm(run)
                    result = tool(
                        "plans.approve",
                        {"run_id": args.value, "revision": run["plan"]["revision"]},
                    )
        elif args.command == "changes":
            change = tool("changes.diff", {"changeset_id": args.id})
            payload = {"changeset_id": args.id, "revision": change["revision"]}
            if args.action == "show":
                result = change
            elif args.action == "edit":
                if not args.file:
                    raise ValueError(
                        "--file containing a proposals JSON array is required"
                    )
                proposals = json.loads(args.file.read_text())
                confirm({"base_revision": change["revision"], "proposals": proposals})
                result = tool("changes.edit", {**payload, "proposals": proposals})
            elif args.action == "validate":
                result = tool("changes.validate", {"changeset_id": args.id})
            elif args.action == "rebase":
                confirm(
                    {
                        "changeset": args.id,
                        "current_base": change["base_revision"],
                        "action": "move this draft onto the current published revision",
                    }
                )
                result = tool("changes.rebase", payload)
            elif args.action == "export":
                confirm(digest_change(change), "Export this committed revision?")
                result = tool("export.run", {"run_id": change["run_id"]})
            else:
                confirm(
                    digest_change(change),
                    "Approve this exact revision?"
                    if args.action == "review"
                    else "Publish this approved revision?",
                )
                result = tool(
                    "changes.approve" if args.action == "review" else "changes.commit",
                    payload,
                )
        elif args.command == "permission":
            pending = api("pilot/permissions")
            if args.action == "list":
                result = pending
            else:
                item = next((p for p in pending if p["id"] == args.id), None)
                if not item or not args.option:
                    raise ValueError("Select a pending permission and --option")
                confirm({**item, "selected": args.option})
                result = api(f"pilot/permissions/{args.id}", {"option_id": args.option})
        elif args.command == "pilot":
            if args.action == "status":
                result = api("pilot/sessions")
            elif not args.id:
                raise ValueError("A run/session ID is required")
            elif args.action == "start":
                if not args.runtime:
                    raise ValueError("--runtime is required")
                result = api(
                    "pilot/sessions", {"run_id": args.id, "runtime": args.runtime}
                )
            elif args.action == "review":
                run = tool("runs.get", {"run_id": args.id})
                change = tool("changes.diff", {"changeset_id": run["changes"]})
                confirm(change)
                result = tool(
                    "plans.review_pilot",
                    {
                        "run_id": args.id,
                        "revision": change["revision"],
                        "accepted": not args.reject,
                        "note": args.text or "Reviewed in human terminal",
                    },
                )
            elif args.action == "message":
                result = api(
                    f"pilot/sessions/{args.id}/message", {"text": args.text or ""}
                )
            else:
                result = api(f"pilot/sessions/{args.id}/{args.action}", {})
        elif args.command == "reset":
            payload = {"dataset": args.dataset}
            preview = tool("workspace.reset", payload)
            if not args.apply:
                result = preview
            else:
                confirm(
                    {
                        "dataset": args.dataset,
                        "runs": len(preview["runs"]),
                        "published_revisions": preview["published_revisions"],
                        "megabytes": round(preview["bytes"] / 1e6, 1),
                        "action": "permanently remove this dataset's agent history",
                    },
                    "Remove this history?",
                )
                result = tool("workspace.reset", {**payload, "apply": True})
        elif args.command == "improvements":
            repo = {"repo_id": args.dataset}
            if args.action == "list":
                result = tool("improvements.list", repo)
            else:
                if not args.slug:
                    raise ValueError("Name the candidate slug")
                candidate = tool("improvements.get", {**repo, "slug": args.slug})
                if args.action == "show":
                    result = candidate
                else:
                    to = {
                        "publish": "published",
                        "reject": "rejected",
                        "retain": "retained",
                        "rollback": "rolled_back",
                        "resolve": "resolved",
                    }[args.action]
                    confirm(
                        {
                            "candidate": args.slug,
                            "state": candidate["state"],
                            "target": candidate["target"],
                            "evaluation": candidate.get("evaluation"),
                            "observations": candidate.get("observations"),
                            "action": f"move to {to}; tasks planned from now on "
                            "run with the result, running tasks keep theirs",
                        },
                        f"Move {args.slug} to {to}?",
                    )
                    result = tool(
                        "improvements.transition",
                        {**repo, "slug": args.slug, "to": to, "note": args.note},
                    )
        elif args.command == "task":
            if args.action == "new":
                result = tool(
                    "tasks.interpret",
                    {
                        "text": args.value,
                        "provider": args.provider,
                        "supervision": args.supervision,
                        "teacher_grant": args.teacher,
                    },
                )
            elif args.action == "show":
                result = tool("tasks.get", {"task_id": args.value})
            elif args.action == "approve":
                task = tool("tasks.get", {"task_id": args.value})
                confirm(
                    {
                        "request": task["request"],
                        "spec": task["spec"],
                        "problems": task["problems"],
                    },
                    "Run this task spec?",
                )
                result = tool("tasks.approve", {"task_id": args.value})
            else:
                result = tool("tasks.advance", {"task_id": args.value})
        elif args.command == "gpu":
            result = tool("gpu.status", {})
        elif args.command == "eval":
            # Written automatically for a run over a whole dataset; this is
            # for any other committed temporal run (a batch, a subset).
            from levi.eval import record
            from levi.harness.ledger import build
            from levi.paths import STATE

            from .runtime import Workbench

            store = Workbench(STATE).store
            run = store.get("runs", args.run)
            if run["status"] != "succeeded":
                p.error(f"{args.run} is {run['status']}; only a committed run")
            if run["context"]["workflow"].get("kind") != "temporal":
                p.error("evaluation records are for subtask (temporal) runs")
            result = {"record": str(record.agent(store, run, build(store, args.run)))}
        elif args.command == "knowledge":
            if args.action in {"list", "refresh"}:
                result = tool(
                    "knowledge.list",
                    {"topic": args.topic, "refresh": args.action == "refresh"},
                )
            else:
                if not args.candidate:
                    p.error("promote and reject need a candidate id")
                if args.action == "promote":
                    confirm(
                        {
                            "candidate": args.candidate,
                            "topic": args.topic,
                            "text": args.text,
                            "action": "append this to LEVI's built-in knowledge "
                            "(a repository file every installation receives)",
                        },
                        "Promote this entry to built-in knowledge?",
                    )
                    result = tool(
                        "knowledge.promote",
                        {
                            "candidate_id": args.candidate,
                            "topic": args.topic,
                            "text": args.text,
                        },
                    )
                    # The docs index the entries; keep it in step.
                    from levi.docs import sync

                    result["docs_updated"] = sync()
                else:
                    result = tool("knowledge.reject", {"candidate_id": args.candidate})
        elif args.command == "memory":
            repo = {"repo_id": args.dataset}
            if args.action == "show":
                result = tool("memory.get", repo)
            elif args.action == "search":
                result = tool("memory.search", {**repo, "query": args.query})
            else:
                confirm(
                    {
                        "dataset": args.dataset,
                        "action": "recompute this dataset's memory from its closed "
                        "runs; published lessons are kept",
                    },
                    "Rebuild this dataset's memory?",
                )
                result = tool("memory.rebuild", repo)
        elif args.command == "clean":
            for run_id in args.abandon:
                tool("runs.abandon", {"run_id": run_id, "reason": args.reason})
            payload = {
                "dataset": args.dataset,
                "older_than_days": args.older_than_days,
                "include_open_runs": args.include_open_runs,
            }
            preview = tool("workspace.clean", payload)
            if not args.apply:
                result = preview
            else:
                confirm({"bytes": preview["bytes"], "runs": len(preview["runs"])})
                result = tool("workspace.clean", {**payload, "apply": True})
        elif args.command == "usage":
            if args.action == "estimate":
                result = tool(
                    "plans.estimate",
                    {
                        "run_id": args.run,
                        "episodes": args.episodes,
                        "evidence_frames": args.frames,
                        "workflow": args.workflow,
                    },
                )
            else:
                from levi.paths import STATE

                from .store import Store
                from .usage import report_table

                result = report_table(Store(STATE))
        elif args.command == "objects":
            if args.action == "relink":
                from backend import app
                from levi.agent.objects import identity_report, link_tracks
                from levi.annotations.schema import ObjectAnnotation

                if not args.dataset:
                    raise ValueError("--dataset is required for relink")
                # The active bundle is whatever the viewer reads, which the
                # backend alone resolves; a second path guess would relink a
                # copy nobody sees.
                store = app._sidecar(
                    app._ensure_state(app.DatasetRef(repo_id=args.dataset))
                )
                current = store.current_revision()
                if not current:
                    raise ValueError(f"{args.dataset} has no published object revision")
                rows = [
                    ObjectAnnotation.model_validate(row)
                    for row in store.read_annotations(current)
                ]
                before = identity_report(rows)
                summary = link_tracks(rows)
                after = identity_report(rows)
                result = {
                    "dataset": args.dataset,
                    "revision": current,
                    "annotations": len(rows),
                    "before": before,
                    "after": after,
                    **summary,
                    "applied": False,
                }
                if args.apply:
                    confirm(
                        {
                            "dataset": args.dataset,
                            "annotations": len(rows),
                            "identity_switches_repaired": summary[
                                "identity_switches_repaired"
                            ],
                            "action": "publish a corrected object revision",
                        }
                    )
                    published = store.publish(rows, parent_revision=current)
                    result["applied"] = True
                    result["published_revision"] = published["revision_id"]
            elif args.action == "status":
                result = (
                    tool("objects.strategy", {"run_id": args.id})
                    if args.id
                    else tool("objects.status", {})
                )
            elif not args.id:
                raise ValueError("An object job ID is required")
            elif args.action == "show":
                result = tool("objects.inspect", {"job_id": args.id})
            elif args.action == "run":
                job = tool("objects.get", {"job_id": args.id})
                confirm({**job, "action": "run the SAM3 worker on this scope"})
                result = tool("objects.run", {"job_id": args.id})
            else:
                decision = "reject" if args.reject_all else "accept"
                if not (args.accept_all or args.reject_all or args.object):
                    raise ValueError(
                        "Choose --accept-all, --reject-all or --object with a decision"
                    )
                scope = tool("objects.get", {"job_id": args.id})["scope"]
                targets = (
                    [{"episode_index": args.episode, "camera_key": args.camera}]
                    if args.episode is not None and args.camera
                    else [
                        {"episode_index": ep, "camera_key": camera}
                        for ep in scope["episodes"]
                        for camera in scope["cameras"]
                    ]
                )
                confirm({"job": args.id, "decision": decision, "scope": targets})
                result = []
                for target in targets:
                    # Each accepted camera advances the staged revision, so the
                    # precondition is re-read instead of assumed.
                    current = tool("objects.inspect", {"job_id": args.id})["revision"]
                    edit = {
                        "episode_index": target["episode_index"],
                        "camera_key": target["camera_key"],
                        "operation": decision,
                        "base_revision": current,
                    }
                    if args.object:
                        edit["object_id"] = args.object
                    result.append(
                        tool("objects.edit", {"job_id": args.id, "edit": edit})
                    )
        elif args.command == "events":
            result = tool("runs.events", {"run_id": args.id, "after": 0})
        elif args.command == "result":
            result = api(f"runs/{args.id}/manifest")
        elif args.command == "open":
            run = tool("runs.get", {"run_id": args.id})
            print(f"Open Agent Workbench, task {args.id}. Start UI: uv run levi")
            # The existing launcher attaches to Core Host; no second executor.
            from levi.paths import PROJECT

            subprocess.Popen([sys.executable, "-m", "levi.cli"], cwd=PROJECT)
            print(f"http://127.0.0.1:7860/?agent_run={run['id']}")
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        p.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
