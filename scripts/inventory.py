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
)


def python() -> str:
    """The tree's own environment when it has one (scripts/dev-env.sh makes it)."""
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


def routes(files: list[str]) -> dict[str, str]:
    """`METHOD /path` -> where. Decorators on app/router objects."""
    found: dict[str, str] = {}
    for f in files:
        if not f.endswith(".py") or f.split("/")[0] not in ("levi", "backend"):
            continue
        try:
            tree = ast.parse(read(f))
        except SyntaxError:
            continue
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
                    found[f"{m} {path}"] = f"{f}:{node.name}"
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


def cli_commands(files: list[str]) -> dict[str, str]:
    """`file name` -> where, for every argparse sub-command."""
    found: dict[str, str] = {}
    pat = re.compile(r"add_parser\(\s*[\"']([\w:-]+)[\"']")
    for f in files:
        if f.endswith(".py") and f.split("/")[0] in ("levi", "backend", "scripts"):
            for m in pat.finditer(read(f)):
                found[f"{f} {m.group(1)}"] = f
    return dict(sorted(found.items()))


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
    title_pat = re.compile(r"""\b(?:test|it)(?:\.\w+)?\(\s*(["'`])((?:\\.|(?!\1).)*)\1""")
    for f in files:
        if re.search(r"\.test\.tsx?$", f):
            titles += [f"{f}::{m.group(2)}" for m in title_pat.finditer(read(f))]
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
    return sorted(l for l in run.stdout.splitlines() if "::" in l)


def build(with_tests: bool) -> dict:
    files = git_files()
    inv = {
        "routes": routes(files),
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
def flatten(inv: dict) -> dict[str, set[str]]:
    """Section -> the set of identities a caller or reader depends on."""
    f = inv.get("frontend", {})
    flat = {
        "routes": set(inv.get("routes", {})),
        "capabilities": set(inv.get("capabilities", {})),
        "cli": set(inv.get("cli", {})),
        "settings": set(inv.get("settings", {})),
        "frontend.pages": set(f.get("pages", [])),
        "frontend.test_titles": set(f.get("test_titles", [])),
        "docs": set(inv.get("docs", {})),
        "python_deps": set(inv.get("dependencies", {}).get("python", [])),
        "node_deps": set(inv.get("dependencies", {}).get("node", {})),
    }
    for lang, keys in f.get("i18n", {}).items():
        flat[f"i18n.{lang}"] = set(keys)
    if "pytest_ids" in inv:
        flat["pytest_ids"] = set(inv["pytest_ids"])
    return flat


def diff(old: dict, new: dict) -> tuple[list[str], list[str]]:
    lost, notes = [], []
    a, b = flatten(old), flatten(new)
    for section in sorted(a):
        if section not in b:
            notes.append(f"{section}: not measured now")
            continue
        for item in sorted(a[section] - b[section]):
            lost.append(f"lost {section}: {item}")
        added = b[section] - a[section]
        if added:
            notes.append(f"{section}: +{len(added)} new")
    for path, digest in old.get("hashed_files", {}).items():
        now = new.get("hashed_files", {}).get(path)
        if now is None:
            lost.append(f"lost file: {path}")
        elif now != digest:
            lost.append(f"changed file: {path}")
    for lang, count in {k: len(v) for k, v in old.get("frontend", {}).get("i18n", {}).items()}.items():
        notes.append(f"i18n.{lang}: {count} -> {len(new.get('frontend', {}).get('i18n', {}).get(lang, []))} keys")
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
