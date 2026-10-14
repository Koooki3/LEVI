#!/usr/bin/env python3
"""Deterministic inventory of what LEVI offers, to prove a refactor lost nothing.

    python3 scripts/inventory.py > before.json            # static sources only
    python3 scripts/inventory.py --with-tests > before.json   # + pytest ids (~10 s)
    python3 scripts/inventory.py --diff before.json       # what disappeared / appeared

Every section is read from the code, never written by hand: HTTP routes, agent
capabilities (``SPECS``), CLI sub-commands, ``LEVI_*`` settings, frontend pages
and API routes, i18n keys, frontend test titles, shipped prompt/knowledge/spec
files (by hash, so an edit shows), AERI schema snapshots, dependency lists and
docs. ``--diff`` exits 1 when anything *disappeared* or a hashed file changed;
additions are reported but are not an error. A refactor that moves a route to
another file is a change of its ``where`` field, not a loss: items are keyed by
what a caller sees (method and path, command name, key), so a move passes and a
deletion does not. Running it changes nothing.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTE_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}
HASHED_DIRS = (
    "levi/agent/skills",
    "levi/knowledge",
    "levi/agent/anchored_specs",
    "docs/architecture",
    "integrations/training_manifest",
    "levi/live/specs",
    "levi/automatic/campaign/templates",
    "levi/events/third_party.json",
)


def python() -> str:
    """The tree's own environment when it has one (scripts/dev-env.sh makes it)."""
    if os.environ.get("LEVI_INVENTORY_PYTHON"):
        return os.environ["LEVI_INVENTORY_PYTHON"]
    venv = ROOT / ".venv/bin/python"
    return str(venv) if venv.exists() else sys.executable


def git_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, text=True, capture_output=True, check=True
    ).stdout
    return [f for f in out.splitlines() if (ROOT / f).is_file()]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf8", errors="replace")


def _str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _router_prefixes(tree: ast.AST) -> dict[str, str]:
    """`name = APIRouter(prefix="/x")` -> {name: "/x"} (literals only)."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
        ):
            for kw in node.value.keywords:
                if kw.arg == "prefix" and (v := _str(kw.value)) is not None:
                    out[node.targets[0].id] = v
    return out


def mounts(files: list[str]) -> dict[str, str]:
    """`app.mount("/x", ...)` and `include_router(r, prefix="/x")` -> where. A router
    mounted under a changed prefix breaks every client even if no route key moves."""
    found: dict[str, str] = {}
    for f in files:
        if not f.endswith(".py") or f.split("/")[0] not in ("levi", "backend"):
            continue
        try:
            tree = ast.parse(read(f))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr == "mount" and node.args and (v := _str(node.args[0])) is not None:
                found[f"mount {v}"] = f
            elif node.func.attr == "include_router":
                prefix = next((_str(k.value) for k in node.keywords if k.arg == "prefix"), None)
                target = ast.unparse(node.args[0]) if node.args else "?"
                found[f"include_router {target} prefix={prefix or ''}"] = f
    return dict(sorted(found.items()))


def routes(files: list[str]) -> dict[str, str]:
    """`METHOD /prefix/path` -> where. Decorators on app/router objects, with the
    router's own `prefix=` literal. Two routers that yield the same key (a bare
    `GET /status` in two files) are told apart by the file: `KEY @file`."""
    pairs: list[tuple[str, str]] = []
    for f in files:
        if not f.endswith(".py") or f.split("/")[0] not in ("levi", "backend"):
            continue
        try:
            tree = ast.parse(read(f))
        except SyntaxError:
            continue
        prefixes = _router_prefixes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                attr = dec.func.attr
                path = _str(dec.args[0]) if dec.args else None
                if path is None:
                    continue
                owner = dec.func.value.id if isinstance(dec.func.value, ast.Name) else ""
                path = prefixes.get(owner, "") + path
                if attr in ROUTE_METHODS:
                    methods = [attr.upper()]
                elif attr == "api_route":
                    methods = ["GET"]
                    for kw in dec.keywords:
                        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                            methods = [m for e in kw.value.elts if (m := _str(e))]
                else:
                    continue
                for m in methods:
                    pairs.append((f"{m} {path}", f"{f}:{node.name}"))
    count: dict[str, int] = defaultdict(int)
    for key, _ in pairs:
        count[key] += 1
    found = {}
    for key, where in pairs:
        found[key if count[key] == 1 else f"{key} @{where.split(':')[0]}"] = where
    return dict(sorted(found.items()))


def capabilities() -> dict[str, str]:
    """Agent capabilities from the registry; empty (with a note) if it cannot import."""
    code = (
        "import json,levi.agent.capabilities as c;"
        "print(json.dumps({k:v[1] for k,v in c.SPECS.items()}))"
    )
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "LEVI_WORKSPACE": tmp, "PYTHONPATH": str(ROOT)}
        run = subprocess.run(
            [python(), "-c", code], cwd=ROOT, env=env, text=True, capture_output=True, check=False
        )
    if run.returncode:
        return {"__error__": run.stderr.strip().splitlines()[-1] if run.stderr else "failed"}
    return dict(sorted(json.loads(run.stdout).items()))


def cli_commands(files: list[str]) -> dict[str, list[str]]:
    """sub-command name -> the files that define one, once per definition. Keyed by
    name, so moving a command to another file is not a loss, but deleting one of
    two same-named commands is (the list gets shorter). Parsers named by a
    variable are not seen."""
    found: dict[str, list[str]] = defaultdict(list)
    pat = re.compile(r"add_parser\(\s*[\"']([\w:-]+)[\"']")
    for f in files:
        if f.endswith(".py") and f.split("/")[0] in ("levi", "backend", "scripts"):
            for m in pat.finditer(read(f)):
                found[m.group(1)].append(f)
    return {k: sorted(v) for k, v in sorted(found.items())}


def settings(files: list[str]) -> dict[str, list[str]]:
    users: dict[str, set[str]] = defaultdict(set)
    pat = re.compile(r"\bLEVI_[A-Z0-9_]+\b")
    for f in files:
        if re.search(r"\.(py|tsx?|toml|md)$", f) and f.split("/")[0] in ("levi", "backend", "src", "scripts"):
            for name in pat.findall(read(f)):
                users[name].add(f)
    return {k: sorted(v) for k, v in sorted(users.items())}


def frontend(files: list[str]) -> dict:
    pages = sorted(
        "/" + re.sub(r"/?(page\.tsx|route\.ts)$", "", f.removeprefix("src/app/"))
        + (" [api]" if f.endswith("route.ts") else "")
        for f in files
        if re.match(r"src/app/.*(page\.tsx|route\.ts)$", f)
    )
    catalogs = {}
    for lang in ("en", "zh"):
        path = ROOT / "src/i18n" / f"{lang}.json"
        if path.is_file():
            catalogs[lang] = sorted(json.loads(path.read_text(encoding="utf8")))
    titles = []
    title_pat = re.compile(r"""\b(?:test|it)(?:\.\w+)?\(\s*(["'`])((?:\\.|(?!\1).)*)\1|\b(?:test|it)\.each\([^)]*\)\(\s*(["'`])((?:\\.|(?!\3).)*)\3""")
    for f in files:
        if re.search(r"\.test\.tsx?$", f):
            titles += [f"{f}::{m.group(2) or m.group(4)}" for m in title_pat.finditer(read(f))]
    return {"pages": pages, "i18n": catalogs, "test_titles": sorted(titles)}


def hashed(files: list[str]) -> dict[str, str]:
    out = {}
    for f in files:
        if any(f.startswith(d + "/") or f == d for d in HASHED_DIRS):
            out[f] = hashlib.sha256((ROOT / f).read_bytes()).hexdigest()[:16]
    return dict(sorted(out.items()))


def dependencies() -> dict:
    import tomllib

    py = tomllib.loads(read("pyproject.toml"))
    pkg = json.loads(read("package.json"))
    return {
        "python_groups": {
            k: sorted(x for x in v if isinstance(x, str))
            for k, v in sorted(py.get("dependency-groups", {}).items())
        },
        "python": sorted(py["project"].get("dependencies", [])),
        "python_extras": {
            k: sorted(v) for k, v in sorted(py["project"].get("optional-dependencies", {}).items())
        },
        "node": dict(sorted(pkg.get("dependencies", {}).items())),
        "node_dev": dict(sorted(pkg.get("devDependencies", {}).items())),
    }


def docs(files: list[str]) -> dict[str, int]:
    """Markdown file -> number of headings (a rewrite that drops a section shows)."""
    return {
        f: len(re.findall(r"^#{1,6} ", read(f), re.MULTILINE))
        for f in files
        if f.endswith(".md") and (f.startswith("docs/") or "/" not in f)
    }


def pytest_ids() -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "LEVI_WORKSPACE": tmp, "PYTHONPATH": str(ROOT), "TMPDIR": tmp}
        run = subprocess.run(
            [python(), "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
             f"--basetemp={tmp}/pt"],
            cwd=ROOT, env=env, text=True, capture_output=True, check=False,
        )
    ids = sorted(line for line in run.stdout.splitlines() if "::" in line)
    if run.returncode not in (0,) or not ids:
        return [f"__error__ pytest collection failed (exit {run.returncode})"]
    return ids


def build(with_tests: bool) -> dict:
    files = git_files()
    inv = {
        "routes": routes(files),
        "mounts": mounts(files),
        "capabilities": capabilities(),
        "cli": cli_commands(files),
        "settings": settings(files),
        "frontend": frontend(files),
        "hashed_files": hashed(files),
        "dependencies": dependencies(),
        "docs": docs(files),
    }
    if with_tests:
        inv["pytest_ids"] = pytest_ids()
    return inv


# ------------------------------------------------------------------------- diff
def _multiset(items) -> set[str]:
    """Repeated identities (two tests with one title, two commands with one name)
    become `x#1`, `x#2`, so deleting one of them shows up as a loss."""
    seen: dict[str, int] = defaultdict(int)
    out = set()
    for item in items:
        seen[item] += 1
        out.add(f"{item}#{seen[item]}")
    return out


def flatten(inv: dict) -> dict[str, set[str]]:
    """Section -> the set of identities a caller or reader depends on."""
    f = inv.get("frontend", {})
    d = inv.get("dependencies", {})
    flat = {
        "routes": set(inv.get("routes", {})),
        "mounts": set(inv.get("mounts", {})),
        "capabilities": set(inv.get("capabilities", {})),
        "cli": _multiset(n for n, files in inv.get("cli", {}).items() for _ in files),
        "settings": set(inv.get("settings", {})),
        "frontend.pages": set(f.get("pages", [])),
        "frontend.test_titles": _multiset(f.get("test_titles", [])),
        # a document is its file and each of its headings (a dropped section shows)
        "docs": {f"{name} heading#{i}" for name, n in inv.get("docs", {}).items() for i in range(max(n, 1))},
        "python_deps": set(d.get("python", [])),
        "python_groups": {f"{g}: {x}" for g, xs in d.get("python_groups", {}).items() for x in xs},
        "python_extras": {f"{g}: {x}" for g, xs in d.get("python_extras", {}).items() for x in xs},
        "node_deps": set(d.get("node", {})),
        "node_dev_deps": set(d.get("node_dev", {})),
    }
    for lang, keys in f.get("i18n", {}).items():
        flat[f"i18n.{lang}"] = set(keys)
    if "pytest_ids" in inv:
        flat["pytest_ids"] = set(inv["pytest_ids"])
    return flat


def diff(old: dict, new: dict) -> tuple[list[str], list[str]]:
    lost, notes = [], []
    for name, getter in (
        ("capabilities", lambda i: i.get("capabilities", {})),
        ("pytest_ids", lambda i: {x: 1 for x in i.get("pytest_ids", [])}),
    ):
        for side, inv in (("old", old), ("new", new)):
            if any(str(k).startswith("__error__") for k in getter(inv)):
                lost.append(f"cannot compare {name}: the {side} inventory recorded an error")
    a, b = flatten(old), flatten(new)
    for section in sorted(a):
        # A section missing on the new side lost everything it had.
        for item in sorted(a[section] - b.get(section, set())):
            lost.append(f"lost {section}: {item}")
        added = b.get(section, set()) - a[section]
        if added:
            notes.append(f"{section}: +{len(added)} new")
    for path, digest in old.get("hashed_files", {}).items():
        now = new.get("hashed_files", {}).get(path)
        if now is None:
            lost.append(f"lost file: {path}")
        elif now != digest:
            lost.append(f"changed file: {path}")
    return lost, notes


def summary(inv: dict) -> str:
    return ", ".join(f"{k} {len(v)}" for k, v in flatten(inv).items())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--with-tests", action="store_true", help="include pytest ids (collects tests)")
    ap.add_argument("--diff", metavar="OLD.json", help="compare the working tree with an earlier inventory")
    ap.add_argument("--summary", action="store_true", help="print counts only")
    args = ap.parse_args()
    if args.diff:
        old = json.loads(Path(args.diff).read_text(encoding="utf8"))
        new = build("pytest_ids" in old)
        lost, notes = diff(old, new)
        print("\n".join(lost + notes) or "identical")
        print(f"\n{len(lost)} lost or changed; now: {summary(new)}")
        return 1 if lost else 0
    inv = build(args.with_tests)
    if args.summary:
        print(summary(inv))
    else:
        print(json.dumps(inv, indent=1, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
