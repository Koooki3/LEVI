"""A wall-clock wait in a test goes through ``scaled()`` (a ratchet, only down).

``time.monotonic() + 5`` gives a slow machine exactly 5 s however slow it is. CI sets
``LEVI_TEST_TIME_SCALE=2`` (tests/conftest.py ``scaled``) because its runners are slower
than the workstation; a literal deadline there fails now and then with nothing wrong in
the code under test (at least 3 of the 6 CI failures of 2026-11-14/15: live_gpu_stop,
marvel0 run_live, vllm_launcher; lab/levi-hub/reports/deflake-int-main-1114.md,
ci-live-proc-1114.md, deflake-round2-1115.md). "Use scaled()" was only a habit, so this
test scans ``tests/**/*.py`` for the two shapes below (A: a literal deadline; B: a
counted sleep loop) and compares the count per file with
``tests/unscaled_deadlines_baseline.json`` (shape A: the file entries at the top level;
shape B: the entries under the key ``"count_loops"``):

- more than the baseline: fail (a new literal deadline);
- fewer than the baseline: fail until the baseline is lowered (so it only goes down);
- a baseline entry whose file is gone: fail.

A. What counts (``ast``, no import of the scanned files). An ``a + b + ...`` sum, outside any
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

B. A counted sleep loop: ``for <name> in range(<integer literal N>):`` whose body (the
innermost loop around the sleep, not a nested function) calls ``time.sleep(<number
literal s>)`` (``sleep(...)`` after ``from time import sleep``, ``t.sleep(...)`` after
``import time as t``; ``asyncio.sleep`` is not looked at) and whose total budget N x s
(summed over the literal sleeps in the body) is at least 1 second. ``for _ in range(100):
time.sleep(0.02)`` waits 2 s however slow the machine is, and nothing scales it with
``LEVI_TEST_TIME_SCALE``. Fine: ``range(int(scaled(100)))``, ``sleep(scaled(0.02))``, a
budget under 1 s, ``range(n)`` with a variable, and a poll on ``time.monotonic() +
scaled(10)``. In a nested loop only the innermost loop around the sleep counts. One loop is
one hit, reported at its ``for`` line. ``# unscaled-ok: <why>`` on the ``for`` line or on
the sleep call's line exempts it.

An intentional use that is not a wait (a token expiry, a fixture timestamp) carries a
``# unscaled-ok: <why>`` comment on its line and is not counted. Do not put a new
violation into the baseline.

    python3 tests/test_no_unscaled_deadlines.py --write-baseline   # after fixing some
    python3 tests/test_no_unscaled_deadlines.py --list             # every current hit

Known gaps (they pass silently): a literal reached through a variable
(``start = time.monotonic(); deadline = start + LIMIT``, ``LIMIT = 5``), a counted loop
whose count or sleep is a variable or a constant (``range(n)``, ``range(LIMIT)``,
``sleep(DELAY)``), a ``range(a, b)`` or ``range(N, 0, -1)`` loop, a ``while n < 100:`` /
``n += 1`` counter, ``itertools.repeat``, a lone ``time.sleep(5)`` outside a loop and
``join(timeout=5)`` with a literal, ``loop.time() + 5``, ``datetime.now() +
timedelta(...)``. The count per file cannot tell one fixed and one added in the same
file apart (both shapes). A counted loop whose sleep does not run every round (it sits
under an ``if``) is still judged by the full count. Standard library only, so the script form needs no environment.
"""

import ast
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).resolve().parent / "unscaled_deadlines_baseline.json"
CLOCKS = {"monotonic", "time", "perf_counter"}
LOOPS_KEY = (
    "count_loops"  # the baseline key that holds shape B; every other key is a file
)
LOOP_BUDGET = 1.0  # seconds: a count loop with at least this total budget is a hit
WAIVER = "unscaled-ok:"
REGEN = "python3 tests/test_no_unscaled_deadlines.py --write-baseline"


class Hit:
    def __init__(self, path: str, line: int, source: str, note: str = ""):
        self.path, self.line, self.source, self.note = path, line, source, note

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


class _CountLoops(ast.NodeVisitor):
    """``for _ in range(<int literal>)`` loops with a literal ``time.sleep`` in the body."""

    def __init__(self, tree: ast.Module):
        self.modules = {"time"}  # names bound to the time module
        self.sleeps: set[str] = set()  # names bound to time.sleep
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "time":
                        self.modules.add(alias.asname or "time")
            elif isinstance(node, ast.ImportFrom) and node.module == "time":
                for alias in node.names:
                    if alias.name == "sleep":
                        self.sleeps.add(alias.asname or "sleep")
        self.found: list[
            tuple[ast.For, int, float, list[ast.Call]]
        ] = []  # for, N, N x s, sleeps
        self._stack: list[list[tuple[ast.Call, float]]] = []

    def _sleep_seconds(self, node: ast.Call) -> float | None:
        func = node.func
        is_sleep = (
            isinstance(func, ast.Name)
            and func.id in self.sleeps
            or isinstance(func, ast.Attribute)
            and func.attr == "sleep"
            and isinstance(func.value, ast.Name)
            and func.value.id in self.modules
        )
        if not is_sleep or len(node.args) != 1 or node.keywords:
            return None
        arg = node.args[0]
        if (
            isinstance(arg, ast.Constant)
            and isinstance(arg.value, (int, float))
            and not isinstance(arg.value, bool)
        ):
            return float(arg.value)
        return None

    def visit_Call(self, node: ast.Call) -> None:
        seconds = self._sleep_seconds(node)
        if seconds is not None and self._stack:
            self._stack[-1].append((node, seconds))
        self.generic_visit(node)

    def _loop(self, node: ast.For | ast.While) -> list[tuple[ast.Call, float]]:
        """Visit the body inside a fresh frame; the sleeps that belong to this loop."""
        self._stack.append([])
        for stmt in node.body:
            self.visit(stmt)
        return self._stack.pop()

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        sleeps = self._loop(node)
        for stmt in node.orelse:
            self.visit(stmt)
        count = _range_count(node)
        if count is not None and sleeps:
            budget = count * sum(seconds for _, seconds in sleeps)
            if round(budget, 9) >= LOOP_BUDGET:
                self.found.append((node, count, budget, [call for call, _ in sleeps]))

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self._loop(
            node
        )  # a while loop owns its sleeps: the outer for does not count them
        for stmt in node.orelse:
            self.visit(stmt)

    def _scope(self, node: ast.AST) -> None:
        saved, self._stack = self._stack, []  # a nested function is not run per round
        self.generic_visit(node)
        self._stack = saved

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _scope
    visit_ClassDef = _scope


def _range_count(node: ast.For) -> int | None:
    """N for ``for <name> in range(N)`` with an integer literal N, else None."""
    call = node.iter
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "range"
        and len(call.args) == 1
        and not call.keywords
        and isinstance(node.target, ast.Name)
    ):
        return None
    arg = call.args[0]
    if (
        isinstance(arg, ast.Constant)
        and isinstance(arg.value, int)
        and not isinstance(arg.value, bool)
    ):
        return arg.value
    return None


def _seconds(value: float) -> str:
    return f"{value:g}"


def count_loops(source: str, path: str = "<source>") -> list[Hit]:
    """Every counted sleep loop with a literal budget of at least 1 s, in line order."""
    tree = ast.parse(source)
    finder = _CountLoops(tree)
    finder.visit(tree)
    lines = source.splitlines()
    hits = []
    for node, count, budget, calls in sorted(finder.found, key=lambda f: f[0].lineno):
        header = lines[node.lineno - 1 : node.body[0].lineno - 1]
        spans = [lines[c.lineno - 1 : (c.end_lineno or c.lineno)] for c in calls] + [
            header
        ]
        if any(WAIVER in line for span in spans for line in span):
            continue
        per_round = budget / count
        note = f"{count} x {_seconds(per_round)} = {_seconds(budget)} s"
        hits.append(Hit(path, node.lineno, lines[node.lineno - 1].strip(), note))
    return hits


def scan_count_loops(root: Path = ROOT) -> dict[str, list[Hit]]:
    """``{path relative to root: hits}`` of ``count_loops`` for every file under ``tests``."""
    found = {}
    for file in sorted((root / "tests").rglob("*.py")):
        rel = file.relative_to(root).as_posix()
        hits = count_loops(file.read_text(encoding="utf-8"), rel)
        if hits:
            found[rel] = hits
    return found


def load_baseline(path: Path = BASELINE) -> dict[str, int]:
    """Shape A: the file entries (every top-level key except ``count_loops``)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if k != LOOPS_KEY}


def load_count_loop_baseline(path: Path = BASELINE) -> dict[str, int]:
    """Shape B: the ``count_loops`` entries (none when the key is absent)."""
    return json.loads(path.read_text(encoding="utf-8")).get(LOOPS_KEY, {})


def write_baseline(
    found: dict[str, list[Hit]],
    path: Path = BASELINE,
    loops: dict[str, list[Hit]] | None = None,
) -> None:
    counts: dict = {rel: len(hits) for rel, hits in found.items()}
    if loops:
        counts[LOOPS_KEY] = {rel: len(hits) for rel, hits in loops.items()}
    partial = path.with_name(path.name + ".partial")
    partial.write_text(
        json.dumps(counts, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
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
    return problems + _stale(root, baseline, found, "unscaled deadline(s)", "")


def _stale(
    root: Path,
    baseline: dict[str, int],
    found: dict[str, list[Hit]],
    noun: str,
    where: str,
) -> list[str]:
    """Baseline entries that are malformed, point at a gone file or are above the count."""
    problems = []
    for rel, allowed in sorted(baseline.items()):
        if not isinstance(allowed, int) or isinstance(allowed, bool) or allowed < 1:
            problems.append(
                f"baseline entry {where}{rel!r} must be a positive integer, got "
                f"{allowed!r}; run `{REGEN}`"
            )
        elif not (root / rel).is_file():
            problems.append(
                f"the baseline lists {where}{rel} but that file no longer exists; run "
                f"`{REGEN}` to drop it"
            )
        elif len(found.get(rel, ())) < allowed:
            problems.append(
                f"{rel} improved: {len(found.get(rel, ()))} {noun}, the "
                f"baseline says {allowed}{' under ' + LOOPS_KEY if where else ''}. Lower "
                f"the baseline so it cannot creep back up: run `{REGEN}`"
            )
    return problems


def check_count_loops(
    root: Path, baseline: dict[str, int], found: dict[str, list[Hit]]
) -> list[str]:
    """Shape B: what is wrong with the counted sleep loops against ``count_loops``."""
    if not isinstance(baseline, dict):
        return [
            (
                f"the baseline key {LOOPS_KEY!r} must map file names to counts, got "
                f"{baseline!r}; run `{REGEN}`"
            )
        ]
    problems = []
    for rel, hits in sorted(found.items()):
        allowed = baseline.get(rel, 0)
        if len(hits) <= allowed:
            continue
        where = "\n".join(
            f"    {rel}:{h.line}: {h.source}   (budget {h.note})" for h in hits
        )
        problems.append(
            f"{rel} has {len(hits)} counted sleep loop(s) with a literal budget of 1 s or "
            f"more, the baseline ({LOOPS_KEY}) allows {allowed}:\n{where}\n"
            "  Why: CI runs with LEVI_TEST_TIME_SCALE=2 because its runners are slower than "
            "the workstation; a fixed number of rounds times a fixed sleep (N x s) is a "
            "wall-clock budget that does not scale with LEVI_TEST_TIME_SCALE, so a slow "
            "machine gets no extra time and fails now and then with nothing wrong in the "
            "code.\n"
            "  Fix: `from conftest import scaled`, then `deadline = time.monotonic() + "
            "scaled(10)` and poll until the condition holds (`while time.monotonic() < "
            "deadline: if cond: break; time.sleep(0.02)`), or write `range(int(scaled(N)))`/"
            "`time.sleep(scaled(s))`. A loop that is not a wait takes `# unscaled-ok: "
            "<why>` on its `for` line or its sleep line. Do not add the new hit to "
            f"{LOOPS_KEY} in tests/unscaled_deadlines_baseline.json."
        )
    return problems + _stale(
        root, baseline, found, "counted sleep loop(s)", f"{LOOPS_KEY}/"
    )


def test_no_new_unscaled_wall_clock_deadlines():
    problems = check(ROOT, load_baseline(), scan())
    assert not problems, "\n\n".join(problems)


def test_no_new_counted_sleep_loops_with_a_literal_budget():
    problems = check_count_loops(ROOT, load_count_loop_baseline(), scan_count_loops())
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


# --- shape B, the counted sleep loop, on synthetic source ------------------------------


def _loop_lines(source: str) -> list[int]:
    return [h.line for h in count_loops(source)]


def test_a_counted_sleep_loop_with_a_literal_budget_of_1_s_or_more_is_a_violation():
    (hit,) = count_loops(
        "import time\nfor _ in range(100):\n    time.sleep(0.02)\n", "t.py"
    )
    assert (hit.line, hit.source, hit.note) == (
        2,
        "for _ in range(100):",
        "100 x 0.02 = 2 s",
    )
    assert _loop_lines("import time\nfor i in range(5):\n    time.sleep(1)\n") == [2]
    # the budget is N x s >= 1 s: exactly 1 s counts, just under does not
    assert _loop_lines("import time\nfor _ in range(10):\n    time.sleep(0.1)\n") == [2]
    assert not _loop_lines("import time\nfor _ in range(9):\n    time.sleep(0.1)\n")
    # one hit per loop, the sleeps of one round add up, an `if` around the sleep still counts
    source = (
        "import time\n"
        "for _ in range(10):\n"
        "    if ready():\n"
        "        break\n"
        "    time.sleep(0.04)\n"
        "    time.sleep(0.06)\n"
    )
    (hit,) = count_loops(source)
    assert (hit.line, hit.note) == (2, "10 x 0.1 = 1 s")


def test_a_small_total_budget_or_a_scaled_or_variable_loop_is_not_a_violation():
    for source in (
        "import time\nfor _ in range(100):\n    time.sleep(0.001)\n",  # 0.1 s in all
        "import time\nfor _ in range(100):\n    time.sleep(scaled(0.02))\n",
        "import time\nfor _ in range(100):\n    time.sleep(c.scaled(0.02))\n",
        "import time\nfor _ in range(int(scaled(100))):\n    time.sleep(0.02)\n",
        "import time\nfor _ in range(n):\n    time.sleep(0.5)\n",  # known gap
        "import time\nfor _ in range(LIMIT):\n    time.sleep(0.5)\n",  # known gap
        "import time\nfor _ in range(100):\n    time.sleep(delay)\n",  # known gap
        "import time\nfor _ in range(100):\n    pass\n",
        "import time\nfor _ in items:\n    time.sleep(0.5)\n",
        "import time\nwhile True:\n    time.sleep(0.5)\n",
        "import time\ntime.sleep(5)\n",  # a lone sleep is another gap
        "import time\nfor _ in range(100):\n    time.sleep(True)\n",
        (
            "import asyncio\nasync def f():\n    for _ in range(100):\n"
            "        await asyncio.sleep(0.5)\n"
        ),
        "import time\nfor _ in range(100):\n    clock.sleep(0.5)\n",
        "from time import monotonic\nfor _ in range(100):\n    sleep(0.5)\n",
    ):
        assert not _loop_lines(source), source


def test_sleep_imports_and_aliases_are_followed_for_count_loops():
    body = "for _ in range(100):\n    {}(0.02)\n"
    assert _loop_lines("from time import sleep\n" + body.format("sleep")) == [2]
    assert _loop_lines("from time import sleep as nap\n" + body.format("nap")) == [2]
    assert _loop_lines("import time as t\n" + body.format("t.sleep")) == [2]
    assert not _loop_lines("import time\n" + body.format("sleep"))  # not imported


def test_in_nested_loops_only_the_innermost_loop_around_the_sleep_counts():
    nested = (
        "import time\n"
        "for _ in range(100):\n"
        "    for _ in range(3):\n"
        "        time.sleep(0.02)\n"
    )
    assert _loop_lines(nested) == []  # the inner loop is 0.06 s, the outer has no sleep
    inner = (
        "import time\n"
        "for _ in range(3):\n"
        "    for _ in range(100):\n"
        "        time.sleep(0.02)\n"
    )
    assert _loop_lines(inner) == [3]  # only the inner loop, not the outer one as well
    while_inside = (
        "import time\n"
        "for _ in range(100):\n"
        "    while not done():\n"
        "        time.sleep(0.02)\n"
    )
    assert _loop_lines(while_inside) == []  # the while loop owns the sleep
    in_function = (
        "import time\n"
        "for _ in range(100):\n"
        "    def later():\n"
        "        time.sleep(0.02)\n"
    )
    assert _loop_lines(in_function) == []  # a nested function is not run each round
    in_else = (
        "import time\nfor _ in range(100):\n    pass\nelse:\n    time.sleep(0.5)\n"
    )
    assert _loop_lines(in_else) == []  # the else clause runs once


def test_a_waiver_exempts_a_count_loop_on_its_for_line_or_its_sleep_line():
    head = "import time\nfor _ in range(100):  # unscaled-ok: a fixture delay\n    time.sleep(0.02)\n"
    assert not _loop_lines(head)
    sleep = "import time\nfor _ in range(100):\n    time.sleep(0.02)  # unscaled-ok: a delay\n"
    assert not _loop_lines(sleep)
    assert _loop_lines(
        "import time\nfor _ in range(100):  # fine\n    time.sleep(0.02)\n"
    ) == [2]
    other = (
        "import time\n"
        "for _ in range(100):  # unscaled-ok: this one\n"
        "    time.sleep(0.02)\n"
        "for _ in range(100):\n"
        "    time.sleep(0.02)\n"
    )
    assert _loop_lines(other) == [4]  # a waiver covers its own loop only


def test_count_loops_and_deadlines_do_not_see_each_other():
    assert not _lines("import time\nfor _ in range(100):\n    time.sleep(0.02)\n")
    assert not _loop_lines("import time\nx = time.monotonic() + 5\n")


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


LOOP2 = (
    "import time\n"
    "for _ in range(100):\n    time.sleep(0.02)\n"
    "for _ in range(5):\n    time.sleep(1)\n"
)
LOOP1 = "import time\nfor _ in range(100):\n    time.sleep(0.02)\n"
LOOP0 = "import time\nfor _ in range(100):\n    time.sleep(scaled(0.02))\n"


def _loop_problems(tmp_path: Path, files: dict[str, str], loops: dict) -> list[str]:
    _tree(tmp_path, files)
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({LOOPS_KEY: loops}), encoding="utf-8")
    return check_count_loops(
        tmp_path, load_count_loop_baseline(path), scan_count_loops(tmp_path)
    )


def test_count_loops_at_their_baseline_are_clean(tmp_path):
    files = {"tests/test_a.py": LOOP2, "tests/b.py": LOOP0}
    assert _loop_problems(tmp_path, files, {"tests/test_a.py": 2}) == []


def test_a_new_count_loop_fails_and_says_where_how_much_why_and_how(tmp_path):
    (problem,) = _loop_problems(
        tmp_path, {"tests/test_a.py": LOOP2}, {"tests/test_a.py": 1}
    )
    for needle in (
        "tests/test_a.py has 2",
        "baseline (count_loops) allows 1",
        "tests/test_a.py:2: for _ in range(100):",
        "tests/test_a.py:4: for _ in range(5):",
        "100 x 0.02 = 2 s",
        "5 x 1 = 5 s",
        "LEVI_TEST_TIME_SCALE=2",
        "does not scale with LEVI_TEST_TIME_SCALE",
        "deadline = time.monotonic() + scaled(10)",
        "# unscaled-ok: <why>",
        "Do not add the new hit to count_loops",
    ):
        assert needle in problem, (needle, problem)


def test_a_file_missing_from_count_loops_has_a_baseline_of_zero(tmp_path):
    (problem,) = _loop_problems(tmp_path, {"tests/test_new.py": LOOP1}, {})
    assert "tests/test_new.py has 1" in problem and "allows 0" in problem


def test_an_improved_count_loop_file_fails_until_the_baseline_is_lowered(tmp_path):
    (problem,) = _loop_problems(
        tmp_path, {"tests/test_a.py": LOOP1}, {"tests/test_a.py": 2}
    )
    assert "tests/test_a.py improved: 1" in problem and "baseline says 2" in problem
    assert "count_loops" in problem and REGEN in problem
    (fixed,) = _loop_problems(
        tmp_path, {"tests/test_a.py": LOOP0}, {"tests/test_a.py": 2}
    )
    assert "improved: 0" in fixed


def test_a_count_loop_baseline_entry_for_a_deleted_file_fails(tmp_path):
    (problem,) = _loop_problems(
        tmp_path, {"tests/test_a.py": LOOP1}, {"tests/test_a.py": 1, "tests/gone.py": 3}
    )
    assert (
        "count_loops/tests/gone.py" in problem
        and "no longer exists" in problem
        and REGEN in problem
    )


def test_a_malformed_count_loop_baseline_fails(tmp_path):
    (problem,) = _loop_problems(
        tmp_path, {"tests/test_a.py": LOOP0}, {"tests/test_a.py": 0}
    )
    assert "must be a positive integer" in problem
    assert check_count_loops(tmp_path, ["tests/test_a.py"], {})  # not a mapping


def test_the_two_baselines_are_read_apart_and_the_old_format_still_loads(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"tests/a.py": 2}), encoding="utf-8")  # an old baseline
    assert load_baseline(path) == {"tests/a.py": 2}
    assert load_count_loop_baseline(path) == {}
    path.write_text(
        json.dumps({"tests/a.py": 2, LOOPS_KEY: {"tests/b.py": 1}}), encoding="utf-8"
    )
    assert load_baseline(path) == {"tests/a.py": 2}  # count_loops is not a file entry
    assert load_count_loop_baseline(path) == {"tests/b.py": 1}


def test_the_baseline_writer_records_count_loops_under_their_own_key(tmp_path):
    _tree(tmp_path, {"tests/z.py": TWO, "tests/a.py": LOOP2 + ONE, "tests/ok.py": NONE})
    path = tmp_path / "baseline.json"
    write_baseline(scan(tmp_path), path, scan_count_loops(tmp_path))
    assert json.loads(path.read_text()) == {
        LOOPS_KEY: {"tests/a.py": 2},
        "tests/a.py": 1,
        "tests/z.py": 2,
    }
    assert (
        path.read_text()
        == json.dumps(json.loads(path.read_text()), indent=2, sort_keys=True) + "\n"
    )
    assert check(tmp_path, load_baseline(path), scan(tmp_path)) == []
    assert (
        check_count_loops(
            tmp_path, load_count_loop_baseline(path), scan_count_loops(tmp_path)
        )
        == []
    )


def main(argv: list[str]) -> int:
    found = scan()
    loops = scan_count_loops()
    if "--write-baseline" in argv:
        write_baseline(found, BASELINE, loops)
        print(
            f"wrote {BASELINE}: {sum(map(len, found.values()))} deadlines in "
            f"{len(found)} files, {sum(map(len, loops.values()))} count loops in "
            f"{len(loops)} files"
        )
        return 0
    if "--list" in argv:
        for hits in found.values():
            for hit in hits:
                print(hit)
        for hits in loops.values():
            for hit in hits:
                print(f"{hit}   [count loop: {hit.note}]")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
