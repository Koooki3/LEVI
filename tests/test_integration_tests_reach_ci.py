"""Every test file under integrations/ reaches a test run, or says where it runs instead.

Found 2026-11-05: three of chunk_accel's four engine suites (test_phase, test_schedulers, test_gripper_resend) were in
neither pyproject's testpaths nor a wrapper under tests/, so CI and the merge tier never ran them and every engine change
needed a separate hand-run step. A test file under integrations/ passes here when it is

* collected: under a pyproject `testpaths` entry;
* exposed: a file under tests/ names it (by path, `integrations/x/test_y.py`, or by module, `integrations.x.test_y`), the
  pattern of tests/marvel0/test_vlash_runtime.py; or
* listed in OWN_ENVIRONMENT with the place it runs instead (a worker's own environment that the core never imports).
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# directory (relative to the checkout) -> where its tests run instead of the core's suite
OWN_ENVIRONMENT = {
    "integrations/recap_value/tests": "the RECAP worker's .venv (unittest discover; integrations/recap_value/README.md)",
    "integrations/sam3/tests": "the SAM3 worker's environment (imports levi_sam3_worker)",
}
SKIP_PARTS = {".venv", "node_modules", "__pycache__", ".pytest_cache"}


def _integration_tests():
    out = []
    for path in sorted((ROOT / "integrations").rglob("test_*.py")):
        rel = path.relative_to(ROOT)
        if SKIP_PARTS.intersection(rel.parts):
            continue
        out.append(rel)
    return out


def _testpaths():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return config["tool"]["pytest"]["ini_options"]["testpaths"]


def _tests_text():
    return "\n".join(
        p.read_text(errors="replace")
        for p in (ROOT / "tests").rglob("*.py")
        if p.name != Path(__file__).name
    )


def test_every_integration_test_file_reaches_a_run():
    paths = [Path(p) for p in _testpaths()]
    text = _tests_text()
    missing = []
    for rel in _integration_tests():
        if any(rel.is_relative_to(p) for p in paths):
            continue
        module = ".".join(rel.with_suffix("").parts)
        if rel.as_posix() in text or re.search(rf"\b{re.escape(module)}\b", text):
            continue
        if any(rel.is_relative_to(d) for d in OWN_ENVIRONMENT):
            continue
        missing.append(rel.as_posix())
    assert not missing, (
        "integration test files that no test run reaches: "
        + ", ".join(missing)
        + ". Add their directory to [tool.pytest.ini_options] testpaths in pyproject.toml, expose them from a file under "
        "tests/ (see tests/marvel0/test_vlash_runtime.py), or list the directory in OWN_ENVIRONMENT of "
        "tests/test_integration_tests_reach_ci.py with the place they run."
    )


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


def test_the_own_environment_list_names_existing_test_directories():
    for directory in OWN_ENVIRONMENT:
        assert any((ROOT / directory).glob("test_*.py")), (
            f"{directory} has no test files: remove it from OWN_ENVIRONMENT"
        )
