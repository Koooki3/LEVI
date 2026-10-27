#!/usr/bin/env python3
"""What a LEVI install without the optional ``agent`` extra must still pass.

The full test suite runs once, with the extra installed. Without it, only two
things can differ: a module that imports an optional package at import time
breaks, and the tests that skip or branch on the extra take the other path.
This script checks exactly those two, so CI and the merge tier do not run the
whole suite a second time to learn them.

    python3 scripts/no_extras_check.py imports   # import every module of levi/ and backend/
    python3 scripts/no_extras_check.py tests     # print the test files that touch the extras

Run ``imports`` and ``pytest $(... tests)`` in an environment synced without
``--extra agent`` (CI's ``python (no extras)`` job; locally
``UV_PROJECT_ENVIRONMENT=.venv-base uv sync --locked --group dev``).
"""

from __future__ import annotations

import importlib
import pkgutil
import re
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("levi", "backend")
# A test file that names an optional package, or skips or branches on whether
# one is installed. Add a name here when an extra gains a package.
EXTRA_PATTERN = re.compile(
    r"pydantic_ai|\bmcp\b|importorskip|find_spec\(|agent extra|extra agent|--extra"
)
# Packages of other optional extras that a module may import at import time:
# ``levi.droid_view`` needs h5py from the ``droid`` extra by design. Nothing
# may need the ``agent`` extra at import time.
OTHER_EXTRAS = {"h5py"}


def modules() -> list[str]:
    names = []
    for package in PACKAGES:
        root = ROOT / package
        names.append(package)
        for info in pkgutil.walk_packages([str(root)], prefix=f"{package}."):
            # A __main__ module runs its program when imported.
            if info.name.rsplit(".", 1)[-1] == "__main__":
                continue
            names.append(info.name)
    return sorted(names)


def check_imports() -> int:
    sys.path.insert(0, str(ROOT))
    failed, optional = [], []
    names = modules()
    for name in names:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as error:
            if (error.name or "").split(".")[0] in OTHER_EXTRAS:
                optional.append(f"{name} (needs {error.name})")
            else:
                failed.append((name, traceback.format_exc(limit=3)))
        except Exception:  # noqa: BLE001 - report every module, not only the first
            failed.append((name, traceback.format_exc(limit=3)))
    for name, trace in failed:
        print(f"FAILED to import {name}\n{trace}")
    for line in optional:
        print(f"skipped, another extra: {line}")
    print(
        f"imported {len(names) - len(failed) - len(optional)} of {len(names)} modules"
    )
    return 1 if failed else 0


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
