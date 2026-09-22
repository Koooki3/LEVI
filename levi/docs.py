"""Keep the documentation true to the code.

``levi docs check`` lists every place the docs have fallen behind the code;
``levi docs sync`` rewrites the parts that are generated from it. The test
suite and ``levi check`` run the check, so a change that adds a capability,
a command, a setting or a knowledge entry fails until its documentation
follows -- the docs iterate with the code instead of after it.

What is checked:

- every agent capability is named in ``docs/AGENTS.md``;
- every ``levi agent`` subcommand is shown somewhere in the docs;
- every user-facing ``LEVI_*`` setting read by the code is documented
  (settings LEVI sets for its own child processes are listed in ``INTERNAL``);
- every relative link in the Markdown files resolves;
- every generated section matches what ``sync`` would write.

Generated sections sit between ``<!-- levi:generated <name> -->`` and
``<!-- /levi:generated <name> -->`` and must not be edited by hand.
"""

import argparse
import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
# Settings LEVI sets itself for the processes it starts; not user-facing.
INTERNAL = {
    "LEVI_AGENT_GRANT_FILE",
    "LEVI_BACKEND_URL",
    "LEVI_CORE_DIR",
    "LEVI_CORE_INSTANCE",
    "LEVI_CORE_PORT",
    "LEVI_FRONTEND_URL",
    "LEVI_PILOT_CHILD",
    "LEVI_UI_TOKEN",
}
MARKER = re.compile(
    r"(<!-- levi:generated (?P<name>[a-z-]+) -->\n)(?P<body>.*?)(<!-- /levi:generated (?P=name) -->)",
    re.DOTALL,
)
LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


def markdown_files() -> list[Path]:
    return sorted(
        [
            *PROJECT.glob("*.md"),
            *(PROJECT / "docs").rglob("*.md"),
            *(PROJECT / "levi/knowledge").glob("*.md"),
        ]
    )


def _all_docs() -> str:
    return "\n".join(p.read_text() for p in markdown_files())


# ------------------------------------------------------------- generators


def _capabilities() -> str:
    from levi.agent.capabilities import SPECS

    who = {"read": "agent", "draft": "agent", "execute": "agent / operator"}
    rows = ["| Capability | Who | What it does |", "| --- | --- | --- |"]
    for name, (_, permission, text) in sorted(SPECS.items()):
        rows.append(
            f"| `{name}` | {who.get(permission, '**person**')} | {' '.join(str(text).split())} |"
        )
    return "\n".join(rows) + "\n"


def _knowledge() -> str:
    from levi.harness import knowledge

    rows = []
    for topic in knowledge.TOPICS:
        entries = knowledge.load(topic)
        rows += [
            f"### {topic} ({len(entries)})",
            "",
            *[f"- **{e['id']}** · {e['text']}" for e in entries],
            "",
        ]
    return "\n".join(rows)


GENERATED = {
    "capabilities": ("docs/API.md", _capabilities),
    "knowledge": ("docs/KNOWLEDGE.md", _knowledge),
}


# ------------------------------------------------------------- check / sync


def _render(text: str, name: str, body: str) -> str:
    return MARKER.sub(lambda m: m[1] + body + m[4] if m["name"] == name else m[0], text)


def check() -> list[str]:
    """Every way the docs disagree with the code, as readable lines."""
    problems = []
    agents = (PROJECT / "docs/AGENTS.md").read_text()
    everything = _all_docs()

    from levi.agent.capabilities import SPECS

    problems += [
        f"docs/AGENTS.md does not name capability `{name}`"
        for name in SPECS
        if f"`{name}`" not in agents
    ]

    from levi.agent.control import build_parser

    commands = next(
        a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)
    )
    problems += [
        f"no document shows `levi agent {name}`"
        for name in commands.choices
        if f"levi agent {name}" not in everything
    ]

    code = "\n".join(
        p.read_text()
        for folder in ("levi", "backend")
        for p in (PROJECT / folder).rglob("*.py")
    )
    settings = set(re.findall(r"LEVI_[A-Z0-9_]+", code)) - INTERNAL
    problems += [
        f"setting `{name}` is read by the code but not documented"
        for name in sorted(settings)
        if name not in everything
    ]

    for path in markdown_files():
        for target in LINK.findall(path.read_text()):
            if target.startswith(("http:", "https:", "mailto:")):
                continue
            if not (path.parent / target).exists():
                problems.append(
                    f"{path.relative_to(PROJECT)} links to missing `{target}`"
                )

    for name, (file, make) in GENERATED.items():
        try:
            text = (PROJECT / file).read_text()
        except OSError:
            problems.append(f"{file} is missing")
            continue
        found = [m for m in MARKER.finditer(text) if m["name"] == name]
        if not found:
            problems.append(f"{file} has no generated section `{name}`")
        elif found[0]["body"] != make():
            problems.append(
                f"{file} section `{name}` is out of date: run `uv run levi docs sync`"
            )
    return problems


def sync() -> list[str]:
    """Rewrite the generated sections; returns the files changed."""
    changed = []
    for name, (file, make) in GENERATED.items():
        path = PROJECT / file
        text = path.read_text()
        updated = _render(text, name, make())
        if updated != text:
            path.write_text(updated)
            changed.append(file)
    return changed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="levi docs", description="Check or regenerate the documentation"
    )
    parser.add_argument("action", choices=["check", "sync"], nargs="?", default="check")
    args = parser.parse_args(argv)
    if args.action == "sync":
        for file in sync():
            print(f"updated {file}")
    problems = check()
    for line in problems:
        print(f"- {line}")
    if not problems:
        print("docs are in step with the code")
    return 1 if problems else 0
