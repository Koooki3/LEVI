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


def confirm(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))
    if (
        not sys.stdin.isatty()
        or input("Approve this exact revision? Type approve: ").strip() != "approve"
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

    identifier = new_id()
    credential = STATE / "agent" / "connections" / identifier / "credential"
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
        "grants", {"client": args.client, "datasets": args.dataset, "hours": args.hours}
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


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["mcp"] or not argv:
        from .mcp import main as serve

        sys.argv = [sys.argv[0]]
        return serve()
    p = argparse.ArgumentParser(description="LEVI local Pilot tools; no UI required")
    commands = p.add_subparsers(dest="command", required=True)
    c = commands.add_parser("core")
    c.add_argument("action", choices=["status", "start", "stop"])
    c = commands.add_parser("connect")
    c.add_argument("--client", choices=["codex", "claude"], required=True)
    c.add_argument("--project", default=".")
    c.add_argument("--dataset", action="append")
    c.add_argument("--hours", type=int, default=24)
    c.add_argument("--apply", action="store_true")
    c = commands.add_parser("disconnect")
    c.add_argument("id")
    c = commands.add_parser("plan")
    c.add_argument("action", choices=["create", "show", "approve"])
    c.add_argument("value")
    c = commands.add_parser("changes")
    c.add_argument(
        "action", choices=["show", "edit", "validate", "review", "commit", "export"]
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
    for name in ["result", "open", "events"]:
        c = commands.add_parser(name)
        c.add_argument("id")
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
        elif args.command == "disconnect":
            result = api(f"grants/{args.id}/revoke", {})
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
            elif args.action == "export":
                confirm(change)
                result = tool("export.run", {"run_id": change["run_id"]})
            else:
                confirm(change)
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
