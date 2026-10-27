#!/usr/bin/env python3
"""What a LEVI install without the optional ``agent`` extra must still pass.

The full test suite runs once, with the extra installed. Without it, a default
install can differ in three ways, and this script checks exactly those, so CI
and the merge tier do not run the whole suite a second time to learn them:

    python3 scripts/no_extras_check.py imports   # every module of levi/ and backend/ imports,
                                                 # and every import statement in them, top level
                                                 # or inside a function, names a package the
                                                 # default install has or one listed in OPTIONAL
    python3 scripts/no_extras_check.py tests     # print the test files that touch the extras

and ``pytest --collect-only`` (every test module still imports), then
``pytest $(... tests)``. Run ``imports`` and pytest in an environment synced
without ``--extra agent``; ``imports`` refuses to run where the extra is
installed. Locally (from a worktree)::

    export UV_PROJECT_ENVIRONMENT=.venv-base
    uv sync --locked --group dev
    uv run --no-sync python scripts/no_extras_check.py imports
    uv run --no-sync pytest --collect-only -q
    uv run --no-sync pytest $(python3 scripts/no_extras_check.py tests)
    unset UV_PROJECT_ENVIRONMENT
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import re
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("levi", "backend")
# Packages that only an optional extra brings, and the files allowed to import
# them (always inside the function that needs them). The agent extra's packages
# must never be imported at import time; ``levi.droid_view`` needs h5py from
# the ``droid`` extra by design.
OPTIONAL = {
    "pydantic_ai": {"levi/agent/providers.py"},
    "openai": {"levi/agent/providers.py"},
    "mcp": {"levi/agent/mcp.py"},
    "h5py": {"levi/droid_view.py", "levi/conversion/inputs/droid_raw.py"},
    # Code that runs in another interpreter: terminal D's openpi environment
    # (fr3_run.py) and the model's own environment (owned_jax.py).
    "scipy": {"levi/automatic/fr3_run.py"},
    "jax": {"levi/live/owned_jax.py"},
}
AGENT_EXTRA = ("pydantic_ai", "mcp")
# A test file that names an optional package, or skips or branches on whether
# one is installed. Add a name here when an extra gains a package.
EXTRA_PATTERN = re.compile(
    r"pydantic_ai|\bopenai\b|\bmcp\b|importorskip|find_spec\(|agent extra|extra agent|--extra"
)


def module_files() -> list[Path]:
    # Every .py file, folders without __init__.py included (pkgutil skips them).
    files = []
    for package in PACKAGES:
        for path in sorted((ROOT / package).rglob("*.py")):
            if path.name != "__main__.py" and "__pycache__" not in path.parts:
                files.append(path)
    return files


def module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imported_tops(path: Path) -> set[str]:
    """The top-level package of every absolute import in the file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    tops = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tops.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            tops.add(node.module.split(".")[0])
    return tops


def check_imports() -> int:
    present = [name for name in AGENT_EXTRA if importlib.util.find_spec(name)]
    if present:
        print(
            f"refused: {', '.join(present)} installed; run this in an environment "
            "synced without --extra agent (see the docstring)"
        )
        return 2
    sys.path.insert(0, str(ROOT))
    failed, optional, stray = [], [], []
    files = module_files()
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        for top in sorted(imported_tops(path)):
            if importlib.util.find_spec(top) is None and rel not in OPTIONAL.get(
                top, ()
            ):
                stray.append(f"{rel} imports {top}, which the default install lacks")
        name = module_name(path)
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as error:
            top = (error.name or "").split(".")[0]
            if rel in OPTIONAL.get(top, ()):
                optional.append(f"{name} (needs {error.name})")
            else:
                failed.append((name, traceback.format_exc(limit=3)))
        except Exception:  # noqa: BLE001 - report every module, not only the first
            failed.append((name, traceback.format_exc(limit=3)))
    for name, trace in failed:
        print(f"FAILED to import {name}\n{trace}")
    for line in stray:
        print(f"FAILED: {line} (add it to OPTIONAL only if it is imported where used)")
    for line in optional:
        print(f"skipped, another extra: {line}")
    print(
        f"imported {len(files) - len(failed) - len(optional)} of {len(files)} modules; "
        f"{len(stray)} import statement(s) outside the default install"
    )
    return 1 if failed or stray else 0


def extra_tests() -> list[str]:
    found = []
    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        if EXTRA_PATTERN.search(path.read_text(encoding="utf-8", errors="replace")):
            found.append(str(path.relative_to(ROOT)))
    return found


def main(argv: list[str]) -> int:
    if argv == ["imports"]:
        return check_imports()
    if argv == ["tests"]:
        tests = extra_tests()
        if not tests:
            print("no test file matches the extras pattern", file=sys.stderr)
            return 1
        print("\n".join(tests))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
