"""Every test file under integrations/ reaches a test run, or says where it runs instead.

Found 2026-11-05: three of chunk_accel's four engine suites (test_phase, test_schedulers, test_gripper_resend) were in
neither pyproject's testpaths nor a wrapper under tests/, so CI and the merge tier never ran them and every engine change
needed a separate hand-run step. A test file under integrations/ (pytest's default names, test_*.py and *_test.py) passes
here when it is

* collected: under a pyproject `testpaths` entry;
* exposed: a module under tests/ imports it (`from integrations.x.test_y import ...`) or loads it by path (a string
  literal `"integrations/x/test_y.py"` in code, as tests/marvel0/test_vlash_runtime.py does); docstrings and comments do
  not count; or
* listed in RUN_ELSEWHERE with where it runs instead, or with the issue that tracks bringing it in.
"""

import ast
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# directory (relative to the checkout) -> where its tests run instead of the core's suite
RUN_ELSEWHERE = {
    "integrations/recap_value/tests": "the RECAP worker's .venv with LEVI_RECAP_FROZEN_SRC "
    "(unittest discover; integrations/recap_value/README.md)",
    "integrations/sam3/tests": "not in CI yet: `python -m unittest discover tests` from integrations/sam3 "
    "(passes in the core .venv; SAM3-WORKER-TESTS-OUTSIDE-CI)",
}
SKIP_PARTS = {".venv", "node_modules", "__pycache__", ".pytest_cache"}


def _integration_tests():
    out = set()
    for pattern in ("test_*.py", "*_test.py"):
        for path in (ROOT / "integrations").rglob(pattern):
            rel = path.relative_to(ROOT)
            if not SKIP_PARTS.intersection(rel.parts):
                out.add(rel)
    return sorted(out)


def _testpaths():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return config["tool"]["pytest"]["ini_options"]["testpaths"]


def _references_from_tests():
    """Module names imported and string literals used in code (docstrings left out) by every module under tests/."""
    modules, strings = set(), set()
    for path in (ROOT / "tests").rglob("*.py"):
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(errors="replace"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
                modules.update(f"{node.module}.{alias.name}" for alias in node.names)
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                strings.add(node.value)
    return modules, strings


def test_every_integration_test_file_reaches_a_run():
    paths = [Path(p) for p in _testpaths()]
    modules, strings = _references_from_tests()
    missing = []
    for rel in _integration_tests():
        if any(rel.is_relative_to(p) for p in paths):
            continue
        if ".".join(rel.with_suffix("").parts) in modules or rel.as_posix() in strings:
            continue
        if any(rel.is_relative_to(d) for d in RUN_ELSEWHERE):
            continue
        missing.append(rel.as_posix())
    assert not missing, (
        "integration test files that no test run reaches: "
        + ", ".join(missing)
        + ". Add their directory to [tool.pytest.ini_options] testpaths in pyproject.toml, import or load them from a "
        "module under tests/ (see tests/marvel0/test_vlash_runtime.py), or list the directory in RUN_ELSEWHERE of "
        "tests/test_integration_tests_reach_ci.py with where they run."
    )


def test_a_mention_in_a_docstring_does_not_count_as_exposed(tmp_path, monkeypatch):
    # the guard must not be satisfied by prose: only an import or a path literal in code exposes a file
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        '"""Mentions integrations/demo/test_demo.py and integrations.demo.test_demo."""\n'
        'X = "integrations/demo/test_loaded.py"\n'
        "from integrations.demo.test_imported import Case\n"
    )
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    modules, strings = _references_from_tests()
    assert "integrations.demo.test_imported" in modules
    assert "integrations/demo/test_loaded.py" in strings
    assert "integrations/demo/test_demo.py" not in strings
    assert "integrations.demo.test_demo" not in modules


def test_the_chunk_accel_engine_suites_are_collected():
    # the engine's four suites must stay in the default run, not only in a hand-run step
    assert "integrations/chunk_accel" in _testpaths()
    names = {
        p.name
        for p in _integration_tests()
        if p.parts[:2] == ("integrations", "chunk_accel")
    }
    assert {
        "test_phase.py",
        "test_schedulers.py",
        "test_gripper_resend.py",
        "test_accel.py",
    } <= names


def test_the_run_elsewhere_list_names_existing_test_directories():
    for directory in RUN_ELSEWHERE:
        assert any((ROOT / directory).glob("test_*.py")), (
            f"{directory} has no test files: remove it from RUN_ELSEWHERE"
        )
