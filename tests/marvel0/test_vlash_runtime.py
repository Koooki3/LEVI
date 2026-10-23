"""Expose isolated runtime regressions to normal CI without importing GPU code."""

import importlib.util
from pathlib import Path

_path = Path(__file__).parents[2] / "integrations/vlash/test_runtime.py"
_spec = importlib.util.spec_from_file_location("vlash_runtime_suite", _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
RuntimeTests = _module.RuntimeTests
