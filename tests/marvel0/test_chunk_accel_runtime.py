"""Expose the isolated chunk-acceleration regressions to normal CI (no openpi, robot or GPU)."""

import ast
import importlib.util
import sys
import unittest
from pathlib import Path

_dir = Path(__file__).parents[2] / "integrations/chunk_accel"
sys.path.insert(0, str(_dir))
_spec = importlib.util.spec_from_file_location(
    "chunk_accel_suite", _dir / "test_accel.py"
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
# Every TestCase of the suite, so a class added there runs here too (review 2026-11-01: RuleV2Tests was left out).
_CASES = {
    name: case
    for name, case in vars(_module).items()
    if isinstance(case, type)
    and issubclass(case, unittest.TestCase)
    and case.__module__ == _module.__name__
}
globals().update(_CASES)


def test_every_test_class_of_the_suite_runs_here():
    tree = ast.parse((_dir / "test_accel.py").read_text())
    defined = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    assert {"RuleTests", "RuleV2Tests", "WrapperTests"} <= defined
    assert set(_CASES) == defined
    assert all(globals()[name] is case for name, case in _CASES.items())


def test_the_page_shows_the_numbers_the_client_uses():
    import accel

    from levi.marvel0 import inference_mode

    assert inference_mode.CHUNK_ACCEL_SPEED_M_S == accel.SPEED
    assert inference_mode.CHUNK_ACCEL_TAU_S == accel.TAU
    # the page offers exactly the client's rules, and its default is the client's (written only when not it)
    assert inference_mode.CHUNK_ACCEL_RULES == accel.RULES
    assert inference_mode.CHUNK_ACCEL_RULE_DEFAULT == accel.ChunkAccel(6, 10).rule
    # the page writes no --accel-rule for v1, so the client's own default must be v1
    source = (_dir / "run_collect_accel.py").read_text()
    assert f'accel_rule: str = "{inference_mode.CHUNK_ACCEL_RULE_DEFAULT}"' in source
    assert inference_mode.CHUNK_ACCEL_CLIENT == "lab/chunk_accel/run_collect_accel.py"
