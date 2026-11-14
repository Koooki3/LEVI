"""A wall-clock deadline in a test goes through ``scaled()`` (a ratchet, only down).

``time.monotonic() + 5`` gives a slow machine exactly 5 s however slow it is. CI sets
``LEVI_TEST_TIME_SCALE=2`` (tests/conftest.py ``scaled``) because its runners are slower
than the workstation; a literal deadline there fails now and then with nothing wrong in
the code under test (at least 3 of the 6 CI failures of 2026-11-14/15: live_gpu_stop,
marvel0 run_live, vllm_launcher; lab/levi-hub/reports/deflake-int-main-1114.md,
ci-live-proc-1114.md, deflake-round2-1115.md). "Use scaled()" was only a habit, so this
test scans ``tests/**/*.py`` for the shapes below and compares the count per file with
``tests/unscaled_deadlines_baseline.json``:

- more than the baseline: fail (a new literal deadline);
- fewer than the baseline: fail until the baseline is lowered (so it only goes down);
- a baseline entry whose file is gone: fail.

What counts (``ast``, no import of the scanned files). An ``a + b + ...`` sum, outside any
``scaled(...)`` call, that has a plain number as a term (``5``, ``0.5``, ``2 * 3``,
``-1``: built from literals only) and either
- a clock read as another term: ``time.monotonic()``, ``time.time()``,
  ``time.perf_counter()``, ``monotonic()`` after ``from time import monotonic``,
  ``t.monotonic()`` after ``import time as t``; or
- an assignment target called ``*deadline*`` whose other terms are only plain names
  (``deadline = start + 5``, ``self.deadline = self.t0 + 5``); a call or a longer
  attribute chain there (``deadline = rig.clock.now() + 1000``) is a simulated clock in
  these tests, not a wall-clock wait, and is left alone.
``time.monotonic() + scaled(5)``, ``time.monotonic() + timeout`` and ``deadline = now +
budget`` are fine.

An intentional use that is not a wait (a token expiry, a fixture timestamp) carries a
``# unscaled-ok: <why>`` comment on its line and is not counted. Do not put a new
violation into the baseline.

    python3 tests/test_no_unscaled_deadlines.py --write-baseline   # after fixing some
    python3 tests/test_no_unscaled_deadlines.py --list             # every current hit

Known gaps (they pass silently): a literal reached through a variable
(``start = time.monotonic(); deadline = start + LIMIT``, ``LIMIT = 5``), a loop that
counts iterations instead of reading a clock, ``time.sleep(5)`` and
``join(timeout=5)`` with a literal, ``loop.time() + 5``, ``datetime.now() +
timedelta(...)``. The count per file cannot tell one fixed and one added in the same
file apart. Standard library only, so the script form needs no environment.
"""

import ast
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).resolve().parent / "unscaled_deadlines_baseline.json"
CLOCKS = {"monotonic", "time", "perf_counter"}
WAIVER = "unscaled-ok:"
REGEN = "python3 tests/test_no_unscaled_deadlines.py --write-baseline"


class Hit:
    def __init__(self, path: str, line: int, source: str):
        self.path, self.line, self.source = path, line, source

    def __repr__(self) -> str:
        return f"{self.path}:{self.line}: {self.source}"


def _number(node: ast.AST) -> bool:
    """A number built from literals only: 5, 0.5, -1, 2 * 3, (10 - 2) / 4."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _number(node.operand)
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Pow)
    ):
        return _number(node.left) and _number(node.right)
    return False


def _terms(node: ast.AST) -> list[ast.AST]:
    """The terms of ``a + b + c`` (however it is parenthesised)."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _terms(node.left) + _terms(node.right)
    return [node]


def _deadline_name(target: ast.AST) -> bool:
    name = (
        target.id
        if isinstance(target, ast.Name)
        else target.attr
        if isinstance(target, ast.Attribute)
        else ""
    )
    return "deadline" in name.lower()


def _plain(node: ast.AST) -> bool:
    """A variable: ``start`` or ``self.t0``."""
    return isinstance(node, ast.Name) or (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


class _Finder(ast.NodeVisitor):
    def __init__(self, tree: ast.Module):
        self.modules = {"time"}  # names bound to the time module
        self.functions = {"monotonic": "monotonic", "perf_counter": "perf_counter"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "time":
                        self.modules.add(alias.asname or "time")
            elif isinstance(node, ast.ImportFrom) and node.module == "time":
                for alias in node.names:
                    if alias.name in CLOCKS:
                        self.functions[alias.asname or alias.name] = alias.name
        self.found: list[ast.BinOp] = []
        self._scaled = 0
        self._deadline_values: set[int] = set()

    def _clock(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call) or node.args or node.keywords:
            return False
        func = node.func
        if isinstance(func, ast.Name):
            return func.id in self.functions
        return (
            isinstance(func, ast.Attribute)
            and func.attr in CLOCKS
            and isinstance(func.value, ast.Name)
            and func.value.id in self.modules
        )

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else ""
        )
        inside = name == "scaled"
        self._scaled += inside
        self.generic_visit(node)
        self._scaled -= inside

    def _assigned(self, targets: list[ast.AST], value: ast.AST | None) -> None:
        if value is not None and any(_deadline_name(t) for t in targets):
            self._deadline_values.add(id(value))

    def visit_Assign(self, node: ast.Assign) -> None:
        self._assigned(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._assigned([node.target], node.value)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._assigned([node.target], node.value)
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if not isinstance(node.op, ast.Add):
            self.generic_visit(node)
            return
        terms = _terms(node)
        if not self._scaled and any(_number(t) for t in terms):
            named = id(node) in self._deadline_values and all(
                _number(t) or _plain(t) for t in terms
            )
            if named or any(self._clock(t) for t in terms):
                self.found.append(node)
        for term in terms:  # the sum itself is judged once, as a whole
            self.visit(term)


def violations(source: str, path: str = "<source>") -> list[Hit]:
    """Every unscaled wall-clock deadline in ``source``, in line order."""
    tree = ast.parse(source)
    finder = _Finder(tree)
    finder.visit(tree)
    lines = source.splitlines()
    hits = []
    for node in sorted(finder.found, key=lambda n: (n.lineno, n.col_offset)):
        span = lines[node.lineno - 1 : (node.end_lineno or node.lineno)]
        if any(WAIVER in line for line in span):
            continue
        hits.append(Hit(path, node.lineno, lines[node.lineno - 1].strip()))
    return hits


def scan(root: Path = ROOT) -> dict[str, list[Hit]]:
    """``{path relative to root: hits}`` for every file under ``root/tests`` with a hit."""
    found = {}
    for file in sorted((root / "tests").rglob("*.py")):
        rel = file.relative_to(root).as_posix()
        hits = violations(file.read_text(encoding="utf-8"), rel)
        if hits:
            found[rel] = hits
    return found


def load_baseline(path: Path = BASELINE) -> dict[str, int]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_baseline(found: dict[str, list[Hit]], path: Path = BASELINE) -> None:
    counts = {rel: len(hits) for rel, hits in sorted(found.items())}
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(counts, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, path)


def check(
    root: Path, baseline: dict[str, int], found: dict[str, list[Hit]]
) -> list[str]:
    """What is wrong with ``found`` against ``baseline``, one message per file."""
    problems = []
    for rel, hits in sorted(found.items()):
        allowed = baseline.get(rel, 0)
        if len(hits) <= allowed:
            continue
        where = "\n".join(f"    {rel}:{h.line}: {h.source}" for h in hits)
        problems.append(
            f"{rel} has {len(hits)} unscaled wall-clock deadline(s), the baseline allows "
            f"{allowed}:\n{where}\n"
            "  Why: CI runs with LEVI_TEST_TIME_SCALE=2 because its runners are slower than "
            "the workstation; a literal deadline gives a slow machine no extra time and "
            "fails now and then with nothing wrong in the code (at least 3 of the 6 CI "
            "failures of 2026-11-14/15).\n"
            "  Fix: `from conftest import scaled` and write `time.monotonic() + "
            "scaled(10)`, or poll until the condition holds instead of waiting for a "
            "time. A use that is not a wait (an expiry, a fixture timestamp) takes "
            "`# unscaled-ok: <why>` on its line. Do not add the new hit to "
            "tests/unscaled_deadlines_baseline.json."
        )
    for rel, allowed in sorted(baseline.items()):
        if not isinstance(allowed, int) or isinstance(allowed, bool) or allowed < 1:
            problems.append(
                f"baseline entry {rel!r} must be a positive integer, got {allowed!r}; "
                f"run `{REGEN}`"
            )
        elif not (root / rel).is_file():
            problems.append(
                f"the baseline lists {rel} but that file no longer exists; run `{REGEN}` "
                "to drop it"
            )
        elif len(found.get(rel, ())) < allowed:
            problems.append(
                f"{rel} improved: {len(found.get(rel, ()))} unscaled deadline(s), the "
                f"baseline says {allowed}. Lower the baseline so it cannot creep back up: "
                f"run `{REGEN}`"
            )
    return problems


def test_no_new_unscaled_wall_clock_deadlines():
    problems = check(ROOT, load_baseline(), scan())
    assert not problems, "\n\n".join(problems)


def test_the_baseline_file_is_what_the_writer_would_produce():
    """Sorted keys, 2-space indent, final newline: a diff shows only real changes."""
    text = BASELINE.read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"


# --- the detector, on synthetic source -------------------------------------------------


def _lines(source: str) -> list[int]:
    return [h.line for h in violations(source)]


def test_a_literal_added_to_a_clock_read_is_a_violation():
    for source in (
        "import time\nx = time.monotonic() + 5\n",
        "import time\nx = time.time() + 2 * 3\n",
        "import time\nx = time.perf_counter() + 0.5\n",
        "import time\nx = 5 + time.monotonic()\n",
        "import time\nx = time.monotonic() + (10 - 2) / 4\n",
        "import time\nx = time.monotonic() + -1\n",
        "import time\nx = time.monotonic() + elapsed + 5\n",
        "import time\nwait(until=time.monotonic() + 30)\n",
    ):
        assert _lines(source), source


def test_scaled_and_names_are_not_violations():
    for source in (
        "import time\nx = time.monotonic() + scaled(5)\n",
        "import time\nx = time.monotonic() + c.scaled(5)\n",
        "import time\nx = time.monotonic() + 2 * scaled(3)\n",
        "import time\nx = scaled(time.monotonic() + 5)\n",
        "import time\ndeadline = time.monotonic() + timeout\n",
        "import time\nx = time.monotonic() + budget + slack\n",
        "import time\nx = time.monotonic() - 5\n",
        "import time\nx = time.monotonic() - start + elapsed\n",
        "x = 1 + 2\n",
        "x = clock.monotonic() + 5\n",
        "x = time() + 5\n",  # a bare `time` that is not from-imported is not a clock
    ):
        assert not _lines(source), source


def test_from_imports_and_aliases_are_followed():
    assert _lines("from time import monotonic\nx = monotonic() + 5\n") == [2]
    assert _lines("from time import monotonic as m\nx = m() + 5\n") == [2]
    assert _lines("from time import time\nx = time() + 5\n") == [2]
    assert _lines("import time as t\nx = t.monotonic() + 5\n") == [2]
    assert not _lines("from time import monotonic\nx = monotonic() + scaled(5)\n")


def test_a_deadline_target_makes_a_literal_sum_a_violation():
    assert _lines("deadline = start + 5\n") == [1]
    assert _lines("self.deadline = now + 0.5\n") == [1]
    assert _lines("hard_deadline: float = base + 60\n") == [1]
    assert _lines("if (deadline := start + 5) > 0: pass\n") == [1]
    assert not _lines("deadline = start + timeout\n")
    assert not _lines("deadline = start + scaled(5)\n")
    assert not _lines("deadline = rig.clock.now() + 1000\n")  # a simulated clock
    assert not _lines("deadline = unit.k.due.due_ns + 1\n")
    assert not _lines("total = start + 5\n")  # not a deadline, not a clock read


def test_one_sum_is_one_hit():
    source = "import time\ndeadline = time.monotonic() + 5\n"
    assert _lines(source) == [2]
    assert _lines("import time\nx = time.monotonic() + 1 + 2 + 3\n") == [2]


def test_nested_and_multiline_uses_are_found_with_their_own_line():
    source = (
        "import time\n"
        "\n"
        "class T:\n"
        "    def test_a(self):\n"
        "        def inner():\n"
        "            return time.monotonic() + 10\n"
        "        async def other():\n"
        "            while time.time() < time.time() + 3:\n"
        "                pass\n"
        "        f(lambda: time.monotonic() + 1)\n"
    )
    assert _lines(source) == [6, 8, 10]
    assert _lines("import time\nx = (\n    time.monotonic()\n    + 5\n)\n") == [3]


def test_a_waiver_comment_on_the_line_exempts_it():
    assert not _lines("import time\nx = time.time() + 3600  # unscaled-ok: expiry\n")
    assert _lines("import time\nx = time.time() + 3600  # fine\n") == [2]
    source = "import time\nx = (\n    time.time()\n    + 5  # unscaled-ok: a stamp\n)\n"
    assert not _lines(source)


def test_the_hit_carries_its_line_text():
    (hit,) = violations(
        "import time\n\n    x = time.monotonic() + 5\n".replace("    x", "x")
    )
    assert (hit.line, hit.source) == (3, "x = time.monotonic() + 5")


# --- the ratchet, on a fake tree and a fake baseline -----------------------------------


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, source in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(source, encoding="utf-8")
    return tmp_path


TWO = "import time\na = time.monotonic() + 5\nb = time.time() + 7\n"
ONE = "import time\na = time.monotonic() + 5\n"
NONE = "import time\na = time.monotonic() + scaled(5)\n"


def _problems(tmp_path: Path, files: dict[str, str], baseline: dict) -> list[str]:
    _tree(tmp_path, files)
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    return check(tmp_path, load_baseline(path), scan(tmp_path))


def test_a_tree_at_its_baseline_is_clean(tmp_path):
    assert (
        _problems(
            tmp_path,
            {"tests/test_a.py": TWO, "tests/b.py": NONE},
            {"tests/test_a.py": 2},
        )
        == []
    )


def test_a_new_violation_fails_and_says_where_why_and_how(tmp_path):
    (problem,) = _problems(tmp_path, {"tests/test_a.py": TWO}, {"tests/test_a.py": 1})
    for needle in (
        "tests/test_a.py has 2",
        "baseline allows 1",
        "tests/test_a.py:2: a = time.monotonic() + 5",
        "tests/test_a.py:3: b = time.time() + 7",
        "LEVI_TEST_TIME_SCALE=2",
        "3 of the 6 CI failures",
        "from conftest import scaled",
        "time.monotonic() + scaled(10)",
        "Do not add the new hit to",
    ):
        assert needle in problem, (needle, problem)


def test_a_file_missing_from_the_baseline_has_a_baseline_of_zero(tmp_path):
    (problem,) = _problems(tmp_path, {"tests/test_new.py": ONE}, {})
    assert "tests/test_new.py has 1" in problem and "baseline allows 0" in problem


def test_an_improved_file_fails_until_the_baseline_is_lowered(tmp_path):
    (problem,) = _problems(tmp_path, {"tests/test_a.py": ONE}, {"tests/test_a.py": 2})
    assert "tests/test_a.py improved: 1" in problem and "baseline says 2" in problem
    assert REGEN in problem
    (fixed,) = _problems(tmp_path, {"tests/test_a.py": NONE}, {"tests/test_a.py": 2})
    assert "improved: 0" in fixed


def test_a_baseline_entry_for_a_deleted_file_fails(tmp_path):
    (problem,) = _problems(
        tmp_path, {"tests/test_a.py": ONE}, {"tests/test_a.py": 1, "tests/gone.py": 3}
    )
    assert (
        "tests/gone.py" in problem
        and "no longer exists" in problem
        and REGEN in problem
    )


def test_a_malformed_baseline_entry_fails(tmp_path):
    (problem,) = _problems(tmp_path, {"tests/test_a.py": NONE}, {"tests/test_a.py": 0})
    assert "must be a positive integer" in problem


def test_the_baseline_writer_sorts_counts_and_renames_a_partial(tmp_path):
    _tree(tmp_path, {"tests/z.py": TWO, "tests/a.py": ONE, "tests/ok.py": NONE})
    path = tmp_path / "baseline.json"
    write_baseline(scan(tmp_path), path)
    assert path.read_text() == '{\n  "tests/a.py": 1,\n  "tests/z.py": 2\n}\n'
    assert not path.with_name("baseline.json.partial").exists()
    assert check(tmp_path, load_baseline(path), scan(tmp_path)) == []


def main(argv: list[str]) -> int:
    found = scan()
    if "--write-baseline" in argv:
        write_baseline(found)
        print(
            f"wrote {BASELINE}: {sum(map(len, found.values()))} in {len(found)} files"
        )
        return 0
    if "--list" in argv:
        for hits in found.values():
            for hit in hits:
                print(hit)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
