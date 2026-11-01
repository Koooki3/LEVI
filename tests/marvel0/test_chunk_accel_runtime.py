"""Expose the isolated chunk-acceleration regressions to normal CI (no openpi, robot or GPU)."""

import ast
import importlib.util
import re
import sys
import unittest
from pathlib import Path

import pytest

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
    # the page offers exactly the ramp client's rules (v3 is another client, below), and its default is the client's
    # (written only when not it)
    assert (
        tuple(r for r in inference_mode.CHUNK_ACCEL_RULES if r != "v3") == accel.RULES
    )
    assert inference_mode.CHUNK_ACCEL_RULES[-1] == "v3"
    assert inference_mode.CHUNK_ACCEL_RULE_DEFAULT == accel.ChunkAccel(6, 10).rule
    # the page writes no --accel-rule for v1, so the client's own default must be v1
    source = (_dir / "run_collect_accel.py").read_text()
    assert f'accel_rule: str = "{inference_mode.CHUNK_ACCEL_RULE_DEFAULT}"' in source
    assert inference_mode.CHUNK_ACCEL_CLIENT == "lab/chunk_accel/run_collect_accel.py"


# ---- rule v3, the phase clock: the page's constants against the engine and the entry point ----

_ROOT = Path(__file__).parents[2]
_COMMAND_LOGIC = _ROOT / "src/components/marvel0/command-logic.ts"


def test_the_page_offers_only_what_the_phase_engine_validates():
    # design section 11: the page constants are a subset of PhaseParams' validated ranges
    import phase

    from levi.marvel0 import inference_mode as page

    assert set(page.CHUNK_ACCEL_V3_ALPHAS) <= set(phase.ALPHAS)
    for lam in page.CHUNK_ACCEL_V3_LAMBDAS:
        for alpha in page.CHUNK_ACCEL_V3_ALPHAS:
            params = phase.PhaseParams.for_page(
                lam, alpha
            )  # raises ValueError out of range
            assert (params.lambda_max, params.alpha) == (lam, alpha)
    # the page's top value is the engine's validated maximum (3.0 since 2026-11-02), never above it
    assert max(page.CHUNK_ACCEL_V3_LAMBDAS) <= 3.0
    with pytest.raises(ValueError):
        phase.PhaseParams.for_page(3.01, 0.005)
    # each default is one of the options, and the options are ascending (a drop-down)
    assert page.CHUNK_ACCEL_V3_LAMBDA_DEFAULT in page.CHUNK_ACCEL_V3_LAMBDAS
    assert page.CHUNK_ACCEL_V3_ALPHA_DEFAULT in page.CHUNK_ACCEL_V3_ALPHAS
    assert list(page.CHUNK_ACCEL_V3_LAMBDAS) == sorted(page.CHUNK_ACCEL_V3_LAMBDAS)
    assert page.CHUNK_ACCEL_V3_ALPHA_DEFAULT == phase.PhaseParams().alpha


def test_rule_v3_names_the_phase_modes_the_engine_runs():
    from levi.marvel0 import inference_mode as page

    assert set(page.CHUNK_ACCEL_V3_MODES) <= set(page.MODES)
    assert page.CHUNK_ACCEL_V3_MODES == ("plain", "rtc", "vlash")


def test_the_phase_entry_point_is_the_installed_file_and_takes_the_pages_flags():
    from levi.marvel0 import command
    from levi.marvel0 import inference_mode as page

    # install.py puts the files of integrations/chunk_accel in openpi/lab/chunk_accel/
    name = page.CHUNK_ACCEL_PHASE_CLIENT.removeprefix("lab/chunk_accel/")
    assert name == "run_collect_phase.py" and "/" not in name
    source = (_dir / name).read_text()
    flags = [flag for flag, _, _ in command.FLAGS if flag.startswith("--phase-")]
    assert flags == ["--phase-mode", "--phase-lambda-max", "--phase-alpha"]
    for flag in flags:
        assert re.search(rf"\b{flag[2:].replace('-', '_')}\b", source), flag


def _typescript_list(name):
    text = _COMMAND_LOGIC.read_text()
    found = re.search(
        rf"export const {name} =\s*\[(.*?)\]\s*as const;", text, re.DOTALL
    )
    assert found, f"{name} is not exported from command-logic.ts as `[...] as const`"
    return re.findall(r'"([^"]*)"', found.group(1))


def test_command_logic_mirrors_the_pages_v3_constants():
    # The page cannot read these from the server (the site answer does not carry them), so command-logic.ts repeats
    # them; this test is the one place that keeps the two lists equal. Change both together.
    from levi.marvel0 import inference_mode as page

    assert tuple(_typescript_list("CHUNK_ACCEL_RULES")) == page.CHUNK_ACCEL_RULES
    assert tuple(_typescript_list("CHUNK_ACCEL_V3_MODES")) == page.CHUNK_ACCEL_V3_MODES
    assert [float(x) for x in _typescript_list("CHUNK_ACCEL_V3_LAMBDAS")] == list(
        page.CHUNK_ACCEL_V3_LAMBDAS
    )
    assert [float(x) for x in _typescript_list("CHUNK_ACCEL_V3_ALPHAS")] == list(
        page.CHUNK_ACCEL_V3_ALPHAS
    )
    text = _COMMAND_LOGIC.read_text()
    alpha = re.search(r'export const CHUNK_ACCEL_V3_ALPHA_DEFAULT = "([^"]*)";', text)
    assert alpha and float(alpha.group(1)) == page.CHUNK_ACCEL_V3_ALPHA_DEFAULT
    # the lambda has no copy: an empty choice sends nothing and the backend's constant applies
    assert "CHUNK_ACCEL_V3_LAMBDA_DEFAULT" not in text


@pytest.mark.parametrize("mode", ["plain", "rtc", "vlash"])
def test_the_default_lambda_is_the_backends_constant_when_the_page_sends_none(
    tmp_path, mode
):
    from site_sample import make_site
    from test_marvel0_command import PROMPT, model

    from levi.marvel0 import command
    from levi.marvel0 import inference_mode as page

    form = command.validate(
        make_site(tmp_path),
        model(),
        {
            "prompt": PROMPT,
            "chunk_accel": True,
            "chunk_accel_rule": "v3",
            "inference_mode": mode,
        },
    )
    assert form.chunk_accel_lambda == page.CHUNK_ACCEL_V3_LAMBDA_DEFAULT
    assert form.chunk_accel_alpha == page.CHUNK_ACCEL_V3_ALPHA_DEFAULT
