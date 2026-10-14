#!/usr/bin/env python3
"""Suggest which tests a change can affect. Report only: it runs nothing.

    python3 scripts/select_tests.py                 # diff against main + working tree
    python3 scripts/select_tests.py --base HEAD~3
    python3 scripts/select_tests.py --files levi/pool/export.py src/utils/x.ts
    python3 scripts/select_tests.py --json
    python3 scripts/select_tests.py --eval-history 100   # how well it would have done

How it decides. Python: an import graph of ``levi/``, ``backend/``, ``scripts/`` and
``tests/`` (static imports, relative imports, bare sibling imports that pytest's
rootdir mode allows, what a conftest fixture imports for the tests that ask for
it, and ``levi.x`` strings, which count as a weak link: started, not imported).
A test is selected when a changed module is reachable from it. TypeScript: the
same over ``src/`` with the ``@/`` alias. A changed data file selects the tests
whose text names it. Anything this cannot place (pyproject, lock files, the root
conftest, build config, shared styles or catalogues, an unmapped data file) is
reported as ``widen`` with the reason: the suggestion then is the whole suite,
never a guess. "Not recognised" must not read as "not affected".

It only suggests. ``--eval-history`` counts the tests that changed *with* the code
that it names; it cannot see a test that broke without being edited, so it does not
prove the suggestion complete. Use the full suite before a merge.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY_DIRS = ("levi", "backend", "scripts", "tests")
# Changes that can alter any test: suggest everything.
PY_WIDEN = (
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "tests/conftest.py",
    "tests/netguard/",
    "tests/fixtures/",
)
TS_WIDEN = (
    "package.json",
    "bun.lock",
    "tsconfig.json",
    "tsconfig.test.json",
    "eslint.config.mjs",
    "next.config.ts",
    "postcss.config.mjs",
    "src/styles/",
    "src/i18n/",
    "scripts/test-isolated.ts",
)
# Documentation has its own check; it only selects tests that name the file.
DOC_CHECK = ("tests/test_docs.py",)
# Scan every shipped file for developer paths and the like.
ALWAYS = ("tests/test_live_portable.py",)
GENERIC_NAMES = {"page.tsx", "layout.tsx", "index.ts", "index.tsx", "route.ts", "__init__.py"}

IMPORT_RE = re.compile(
    r"""(?:import|export)\s[^'"`;]*?from\s*['"]([^'"]+)['"]|"""
    r"""(?:import|require)\s*\(\s*['"]([^'"]+)['"]\s*\)|"""
    r"""^\s*import\s*['"]([^'"]+)['"]|"""
    r"""mock\.module\(\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)
LEVI_STR_RE = re.compile(r"\b(?:levi|backend|scripts)(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


def git(*args: str, strict: bool = True) -> str:
    """Run git. A failure is an error, not an empty answer: a wrong --base or a
    checkout without a local `main` (CI pull requests) must not read as "nothing
    changed"."""
    run = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=False)
    if run.returncode and strict:
        sys.exit(f"select_tests: git {' '.join(args)} failed: {run.stderr.strip()}")
    return run.stdout


def tracked() -> list[str]:
    return [p for p in git("ls-files").splitlines() if (ROOT / p).is_file()]


# ----------------------------------------------------------------------- python
def module_of(path: str) -> str:
    parts = Path(path).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


class PyGraph:
    def __init__(self, files: list[str]):
        self.files = [f for f in files if f.endswith(".py") and f.split("/")[0] in PY_DIRS]
        self.by_module = {module_of(f): f for f in self.files}
        self.text: dict[str, str] = {}
        # file -> files it needs. "strong": imported when the module loads;
        # "weak": imported inside a function, only when that code path runs.
        self.imports: dict[str, set[str]] = defaultdict(set)
        self.weak: dict[str, set[str]] = defaultdict(set)
        self.importers: dict[str, set[str]] = defaultdict(set)
        self.weak_importers: dict[str, set[str]] = defaultdict(set)
        self.fixtures: dict[str, dict[str, tuple[set[str], set[str]]]] = {}
        self.autouse: dict[str, set[str]] = {}
        self.test_stems: dict[str, set[str]] = defaultdict(set)
        for f in self.files:
            if f.startswith("tests/") and not is_test(f) and Path(f).name != "conftest.py":
                self.test_stems[Path(f).stem].add(f)
        for f in self.files:
            self._scan(f)
        self._add_fixture_edges()
        for f, deps in self.imports.items():
            for d in deps:
                self.importers[d].add(f)
        for f, deps in self.weak.items():
            for d in deps - self.imports[f]:
                self.weak_importers[d].add(f)

    def _resolve(self, dotted: str) -> str | None:
        """Longest existing module prefix, so `levi.a.b.func` finds `levi.a.b`."""
        parts = dotted.split(".")
        while parts:
            hit = self.by_module.get(".".join(parts))
            if hit:
                return hit
            parts.pop()
        return None

    def _record_fixtures(self, f: str, tree: ast.AST, pkg: str) -> None:
        found: dict[str, tuple[set[str], set[str]]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any("fixture" in ast.unparse(d) for d in node.decorator_list):
                continue
            mods: set[str] = set()
            for sub in ast.walk(node):
                names: list[str] = []
                if isinstance(sub, ast.Import):
                    names = [a.name for a in sub.names]
                elif isinstance(sub, ast.ImportFrom) and not sub.level and sub.module:
                    names = [sub.module] + [f"{sub.module}.{a.name}" for a in sub.names]
                for n in names:
                    hit = self._resolve(n)
                    if hit and hit != f:
                        mods.add(hit)
            params = {a.arg for a in node.args.args + node.args.kwonlyargs}
            found[node.name] = (mods, params)
            if any("autouse=True" in ast.unparse(d) for d in node.decorator_list):
                self.autouse.setdefault(f, set()).add(node.name)
        self.fixtures[f] = found

    def _scan(self, f: str) -> None:
        text = (ROOT / f).read_text(encoding="utf8", errors="replace")
        self.text[f] = text
        try:
            tree = ast.parse(text)
        except SyntaxError:
            self.imports[f].add("*")  # cannot analyse: caller widens
            return
        here = module_of(f)
        pkg = here if Path(f).name == "__init__.py" else here.rpartition(".")[0]
        sibling_dir = str(Path(f).parent)
        strong, weak = self.imports[f], self.weak[f]
        deps = strong

        def add(dotted: str) -> None:
            hit = self._resolve(dotted)
            if hit and hit != f:
                deps.add(hit)
            # Importing a.b.c runs the __init__ of a and a.b first.
            parts = dotted.split(".")
            for i in range(1, len(parts)):
                parent = self.by_module.get(".".join(parts[:i]))
                if parent and parent != f and parent.endswith("__init__.py"):
                    deps.add(parent)

        def bare(name: str) -> None:
            # pytest's rootdir mode puts the test file's directory on sys.path.
            stem = name.split(".")[0]
            cand = Path(sibling_dir) / (stem + ".py")
            if (ROOT / cand).is_file() and cand.as_posix() != f:
                deps.add(cand.as_posix())
            elif f.startswith("tests/") and len(self.test_stems.get(stem, ())) == 1:
                # a helper in another tests/ directory (pytest's rootdir mode)
                (only,) = self.test_stems[stem]
                if only != f:
                    deps.add(only)

        if Path(f).name == "conftest.py":
            self._record_fixtures(f, tree, pkg)
        lazy = {
            id(n)
            for fn in ast.walk(tree)
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            for n in ast.walk(fn)
        }
        for node in ast.walk(tree):
            deps = weak if id(node) in lazy else strong
            if isinstance(node, ast.Import):
                for a in node.names:
                    add(a.name)
                    bare(a.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = pkg.split(".")
                    base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                    prefix = ".".join(base + ([node.module] if node.module else []))
                else:
                    prefix = node.module or ""
                    bare(prefix)
                if prefix:
                    add(prefix)
                    for a in node.names:  # `from levi import report` is a module
                        add(f"{prefix}.{a.name}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # A module named in a string (uvicorn "levi.service:app", a
                # subprocess "-m levi.cli") is started, not imported: weak.
                deps = weak
                for m in LEVI_STR_RE.findall(node.value):
                    add(m)

    def direct(self, changed: set[str]) -> set[str]:
        """Files that import a changed file, any way, one hop."""
        out = set(changed)
        for c in changed:
            out |= self.importers.get(c, set()) | self.weak_importers.get(c, set())
        return out

    def _add_fixture_edges(self) -> None:
        """A test that asks for a conftest fixture depends on what the fixture
        imports, even when it does so inside its body (`client` imports the
        service). Fixtures asking for fixtures are followed."""

        def deps_of(conf: str, name: str, seen: set[str]) -> set[str]:
            if name in seen or name not in self.fixtures.get(conf, {}):
                return set()
            seen.add(name)
            mods, params = self.fixtures[conf][name]
            out = set(mods)
            for p in params:
                out |= deps_of(conf, p, seen)
            return out

        for f in self.files:
            if not is_test(f):
                continue
            try:
                tree = ast.parse(self.text[f])
            except SyntaxError:
                continue
            confs = [
                c
                for c in self.fixtures
                if Path(f).as_posix().startswith(str(Path(c).parent.as_posix()) + "/")
            ]
            # An autouse fixture applies to every test below its conftest, asked for
            # or not.
            for c in confs:
                for name in self.autouse.get(c, ()):
                    self.imports[f] |= deps_of(c, name, set())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for arg in node.args.args + node.args.kwonlyargs:
                        for c in confs:
                            self.imports[f] |= deps_of(c, arg.arg, set())

    def closure(self, changed: set[str]) -> set[str]:
        """Strong imports are followed transitively; an importer that only reaches a
        changed file from inside a function is added but not expanded, because it
        breaks only if that path runs (a hub like service.py would otherwise pull
        in every test)."""
        seen, todo = set(changed), list(changed)
        while todo:
            x = todo.pop()
            for imp in self.importers.get(x, ()):
                if imp not in seen:
                    seen.add(imp)
                    todo.append(imp)
            seen |= self.weak_importers.get(x, set())
        return seen


def is_test(path: str) -> bool:
    return bool(re.search(r"(^|/)test_[^/]*\.py$", path))


# -------------------------------------------------------------------- typescript
class TsGraph:
    def __init__(self, files: list[str]):
        self.files = [f for f in files if f.startswith("src/") and re.search(r"\.(tsx?|css|json)$", f)]
        self.set = set(self.files)
        self.importers: dict[str, set[str]] = defaultdict(set)
        for f in self.files:
            if not re.search(r"\.tsx?$", f):
                continue
            text = (ROOT / f).read_text(encoding="utf8", errors="replace")
            for m in IMPORT_RE.finditer(text):
                spec = next(g for g in m.groups() if g)
                target = self._resolve(f, spec)
                if target and target != f:
                    self.importers[target].add(f)

    def _resolve(self, importer: str, spec: str) -> str | None:
        if spec.startswith("@/"):
            base = Path("src") / spec[2:]
        elif spec.startswith("."):
            base = Path(importer).parent / spec
        else:
            return None
        base = Path(*[p for p in base.as_posix().split("/")])
        norm: list[str] = []
        for part in base.parts:
            if part == "..":
                if norm:
                    norm.pop()
            elif part != ".":
                norm.append(part)
        stem = "/".join(norm)
        for cand in (
            stem,
            f"{stem}.ts",
            f"{stem}.tsx",
            f"{stem}.json",
            f"{stem}.css",
            f"{stem}/index.ts",
            f"{stem}/index.tsx",
        ):
            if cand in self.set:
                return cand
        return None

    def closure(self, changed: set[str]) -> set[str]:
        seen, todo = set(changed), list(changed)
        while todo:
            for imp in self.importers.get(todo.pop(), ()):
                if imp not in seen:
                    seen.add(imp)
                    todo.append(imp)
        return seen


def is_ts_test(path: str) -> bool:
    return bool(re.search(r"\.test\.tsx?$", path))


# --------------------------------------------------------------------- selection
def select(changed: list[str], files: list[str], graphs: tuple | None = None) -> dict:
    py, ts = graphs or (PyGraph(files), TsGraph(files))
    all_py_tests = sorted(f for f in py.files if is_test(f))
    all_ts_tests = sorted(f for f in ts.files if is_ts_test(f))
    out = {
        "changed": changed,
        "pytest": {"tests": [], "total": len(all_py_tests), "widen": []},
        "bun": {"tests": [], "total": len(all_ts_tests), "widen": []},
        "other": [],
    }
    py_changed: set[str] = set()
    ts_changed: set[str] = set()
    data_changed: list[str] = []
    named_by_path: list[str] = []
    named_by_deleted: set[str] = set()
    for c in changed:
        if any(c == w or c.startswith(w) for w in PY_WIDEN):
            out["pytest"]["widen"].append(f"{c}: shared by every Python test")
        if any(c == w or c.startswith(w) for w in TS_WIDEN):
            out["bun"]["widen"].append(f"{c}: shared by every frontend test")
        if c.endswith(".py") and c.split("/")[0] in PY_DIRS:
            if c in py.imports and "*" in py.imports[c]:
                out["pytest"]["widen"].append(f"{c}: cannot be parsed")
            if c not in py.text:
                # deleted or renamed away: whatever still names it must be found
                hits = [t for t in all_py_tests if module_of(c) in py.text[t] or Path(c).name in py.text[t]]
                named_by_deleted.update(hits)
                if not hits:
                    out["pytest"]["widen"].append(f"{c}: no longer exists and no test names it")
            py_changed.add(c)
            named_by_path.append(c)
        elif re.search(r"\.(tsx?|css)$", c) and c.startswith("src/"):
            ts_changed.add(c)
            named_by_path.append(c)
        elif c.endswith(".md") and c.startswith(("docs/", "README", "CHANGELOG")):
            out["other"].append(f"{c}: documentation: run `uv run levi docs check`")
            data_changed.append(c)
        elif c.startswith(("levi/", "backend/", "tests/", "docs/", "src/")):
            data_changed.append(c)
        else:
            out["other"].append(f"{c}: not mapped to a test group (CI/config/other)")
            data_changed.append(c)
            for key in ("pytest", "bun"):
                out[key]["widen"].append(f"{c}: not mapped to a test group")

    selected = py.closure(py_changed)
    # A deleted or renamed module is in no import graph, but a test that still
    # names it is the first to break.
    selected |= named_by_deleted
    # Helper and tool scripts under tests/ and scripts/ are often started by path
    # (a subprocess), which no import shows: a test that names the file counts.
    for c in py_changed:
        if c.split("/")[0] in ("tests", "scripts") and not is_test(c):
            selected |= {t for t in all_py_tests if Path(c).name in py.text[t]}
    ts_sel = ts.closure(ts_changed)
    # A changed data file selects the tests that name it (by path or file name).
    for d in data_changed:
        name, hits = Path(d).name, set()
        for t in all_py_tests:
            if d in py.text[t] or name in py.text[t]:
                hits.add(t)
        for t in all_ts_tests:
            text = (ROOT / t).read_text(encoding="utf8", errors="replace")
            if d in text or name in text:
                ts_sel.add(t)
                hits.add(t)
        selected |= {h for h in hits if h in py.text}
        docs_only = d.endswith(".md") and d.startswith(("docs/", "README", "CHANGELOG"))
        if not hits and not docs_only:
            out["pytest"]["widen"].append(f"{d}: no test names this data file")
        if d.startswith("src/") and re.search(r"\.json$", d):
            ts_sel |= ts.closure({d})
    # Sources a test reads by path (a stylesheet, a page, a script) are not imported.
    ts_text = {t: (ROOT / t).read_text(encoding="utf8", errors="replace") for t in all_ts_tests}
    for c in named_by_path:
        parts = Path(c).parts
        # Tests usually build the path from pieces (join(dir, "../globals.css"),
        # "explore/page.tsx"), so the last two components count as naming it.
        needles = {c, "/".join(parts[-2:])}
        if c.endswith(".css") and Path(c).name not in GENERIC_NAMES:
            needles.add(Path(c).name)
        for t in all_py_tests:
            if any(n in py.text[t] for n in needles):
                selected.add(t)
        for t, text in ts_text.items():
            if any(n in text for n in needles):
                ts_sel.add(t)
    if any(d.endswith(".md") for d in data_changed):
        selected |= {t for t in DOC_CHECK if t in py.text}
    # Guards that scan every shipped file run whenever shipped code or data changes.
    if any(not is_test(c) and not is_ts_test(c) for c in changed):
        selected |= {t for t in ALWAYS if t in py.text}
    out["pytest"]["tests"] = sorted(t for t in selected if is_test(t))
    out["bun"]["tests"] = sorted(t for t in ts_sel if is_ts_test(t))
    return out


def changed_files(base: str) -> list[str]:
    # --no-renames: a moved file counts as the old path deleted and the new one added,
    # so whatever still imports the old name is looked for.
    names = set(git("diff", "--name-only", "--no-renames", f"{base}...HEAD").splitlines())
    names |= set(git("diff", "--name-only", "--no-renames", "HEAD").splitlines())
    names |= set(git("ls-files", "-o", "--exclude-standard").splitlines())
    return sorted(n for n in names if n)


def render(res: dict) -> str:
    lines = []
    for key, label, cmd in (
        ("pytest", "Python", "uv run pytest"),
        ("bun", "Frontend", "bun run test"),
    ):
        g = res[key]
        if g["widen"]:
            lines.append(f"{label}: WIDEN -> run all {g['total']} test files")
            lines += [f"    {w}" for w in g["widen"]]
        elif not g["tests"]:
            lines.append(f"{label}: no test reaches the changed files")
        else:
            lines.append(f"{label}: {len(g['tests'])} of {g['total']} test files")
            lines.append(f"    {cmd} " + " ".join(g["tests"]))
    lines += [f"note: {o}" for o in res["other"]]
    lines.append("(suggestion only; the merge gate stays the full suite)")
    return "\n".join(lines)


def evaluate(n: int, files: list[str]) -> str:
    """For recent commits that changed code and tests, how many of the tests that
    changed with the code does the selector name, from the code alone?"""
    log = git("log", "--no-merges", f"-{n * 6}", "--format=%H", strict=False).split()
    seen = hit = miss = commits = widened = 0
    ratios = []
    pygraph_files = set(files)
    graphs = (PyGraph(files), TsGraph(files))
    missed: list[str] = []
    for sha in log:
        names = git("show", "--name-only", "--format=", sha, strict=False).split()
        tests = [x for x in names if (is_test(x) or is_ts_test(x)) and x in pygraph_files]
        code = [x for x in names if x in pygraph_files and not is_test(x) and not is_ts_test(x)
                and re.search(r"\.(py|tsx?)$", x) and not x.endswith("select_tests.py")]
        if not tests or not code:
            continue
        res = select(code, files, graphs)
        picked = set(res["pytest"]["tests"]) | set(res["bun"]["tests"])
        full = bool(res["pytest"]["widen"] or res["bun"]["widen"])
        commits += 1
        widened += full
        for t in tests:
            seen += 1
            if full or t in picked:
                hit += 1
            else:
                miss += 1
                missed.append(f"{sha[:8]} {t} (code: {', '.join(code[:3])})")
        total = res["pytest"]["total"] + res["bun"]["total"]
        ratios.append(1.0 if full else len(picked) / max(1, total))
        if commits >= n:
            break
    if not commits:
        return "no commits with both code and tests found"
    avg = sum(ratios) / len(ratios)
    detail = "".join(f"\n  missed: {m}" for m in missed[:10])
    return (
        f"{commits} commits, {seen} co-changed test files: named {hit}, missed {miss} "
        f"(recall {hit / max(1, seen):.0%}); widened to full in {widened}; "
        f"mean share of test files suggested {avg:.0%}. "
        "Files are read from the current tree, so deleted or renamed ones are skipped."
        + detail
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="main")
    ap.add_argument("--files", nargs="*")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--eval-history", type=int, metavar="N")
    args = ap.parse_args()
    files = tracked()
    if args.eval_history:
        print(evaluate(args.eval_history, files))
        return 0
    changed = sorted(args.files) if args.files else changed_files(args.base)
    if not changed:
        print("no changed files")
        return 0
    res = select(changed, files)
    print(json.dumps(res, indent=1) if args.json else render(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
