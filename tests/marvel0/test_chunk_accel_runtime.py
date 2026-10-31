"""Expose the isolated chunk-acceleration regressions to normal CI (no openpi, robot or GPU)."""

import importlib.util
import sys
from pathlib import Path

_dir = Path(__file__).parents[2] / "integrations/chunk_accel"
sys.path.insert(0, str(_dir))
_spec = importlib.util.spec_from_file_location(
    "chunk_accel_suite", _dir / "test_accel.py"
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
RuleTests = _module.RuleTests
WrapperTests = _module.WrapperTests


def test_the_page_shows_the_numbers_the_client_uses():
    import accel

    from levi.marvel0 import inference_mode

    assert inference_mode.CHUNK_ACCEL_SPEED_M_S == accel.SPEED
    assert inference_mode.CHUNK_ACCEL_TAU_S == accel.TAU
    assert inference_mode.CHUNK_ACCEL_CLIENT == "lab/chunk_accel/run_collect_accel.py"
