"""The arm controller choice in the control file (``arm_controller``,
``arm_preset``, ``anyspeedup_root`` of ``[stack]``, ``levi/marvel0/control_site.py``):
``lab`` is the default and changes nothing; ``anyspeedup`` names the preset whose
setpoint filter alpha the client's v3 phase clock is forced to; ``S2`` is refused
until the engine has the shaped profile it needs. The anyspeedup checkout is read
(read-only) only when it is beside this one, to keep the preset table honest."""

import ast
import tomllib
from pathlib import Path

import pytest
from arm_controller_helpers import ANY_ROOT, anyspeedup_checkout
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard
from test_marvel0_stack_act_control import GOOD, codes, read_file

from levi.marvel0 import control_site as cs
from levi.marvel0 import inference_mode

# --- the control file -----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_cache():
    cs.clear_cache()
    yield
    cs.clear_cache()


def with_arm(text):
    return GOOD.replace("marker_wait_s = 60\n", "marker_wait_s = 60\n" + text, 1)


def test_without_the_keys_the_lab_controller_is_chosen_and_nothing_is_forced(tmp_path):
    loaded = read_file(tmp_path, GOOD)
    stack_control = loaded.site.stack
    assert stack_control.arm_controller == "lab" and not stack_control.anyspeedup
    assert stack_control.arm_preset_in_use is None and stack_control.arm_alpha is None
    assert cs.view(loaded)["arm"] == {
        "controller": "lab",
        "preset": None,
        "alpha": None,
    }
    template = cs.parse(tomllib.loads(cs.TEMPLATE)).stack
    assert template.arm_controller == "lab" and template.anyspeedup_root is None


@pytest.mark.parametrize("preset, alpha", [("today", 0.005), ("S1", 0.01)])
def test_the_anyspeedup_controller_with_a_preset_forces_that_presets_alpha(
    tmp_path, preset, alpha
):
    loaded = read_file(
        tmp_path,
        with_arm(
            f'arm_controller = "anyspeedup"\narm_preset = "{preset}"\n'
            f'anyspeedup_root = "{ANY_ROOT}"\n'
        ),
    )
    assert loaded.state == "ok", loaded.errors
    stack_control = loaded.site.stack
    assert stack_control.anyspeedup and stack_control.arm_preset_in_use == preset
    assert stack_control.anyspeedup_root == ANY_ROOT
    assert stack_control.arm_alpha == alpha
    assert cs.view(loaded)["arm"] == {
        "controller": "anyspeedup",
        "preset": preset,
        "alpha": alpha,
    }
    assert cs.arm_alpha({cs.ENV: str(tmp_path / "control.toml")}) == alpha


def test_the_preset_defaults_to_today(tmp_path):
    loaded = read_file(
        tmp_path,
        with_arm(f'arm_controller = "anyspeedup"\nanyspeedup_root = "{ANY_ROOT}"\n'),
    )
    assert loaded.site.stack.arm_preset == "today"
    assert loaded.site.stack.arm_alpha == 0.005


def test_a_stack_that_is_off_forces_nothing_even_with_the_anyspeedup_controller(
    tmp_path,
):
    text = with_arm(
        f'arm_controller = "anyspeedup"\narm_preset = "S1"\nanyspeedup_root = "{ANY_ROOT}"\n'
    ).replace("enabled = true\nfranka", "enabled = false\nfranka", 1)
    loaded = read_file(tmp_path, text)
    assert loaded.state == "ok" and loaded.site.stack.enabled is False
    assert loaded.site.stack.arm_alpha is None, "the page does not start A: not known"
    assert cs.view(loaded)["arm"]["controller"] == "anyspeedup"
    assert cs.arm_alpha({cs.ENV: str(tmp_path / "control.toml")}) is None


def test_arm_alpha_is_none_without_a_valid_file(tmp_path):
    assert cs.arm_alpha({cs.ENV: str(tmp_path / "absent.toml")}) is None
    write = tmp_path / "control.toml"
    write.write_text("not toml [[[")
    write.chmod(0o644)
    assert cs.arm_alpha({cs.ENV: str(write)}) is None
    assert cs.view(cs.read({cs.ENV: str(write)}))["arm"] is None
    missing = cs.read({cs.ENV: str(tmp_path / "absent.toml")})
    assert cs.view(missing)["arm"] is None


@pytest.mark.parametrize(
    "line, field, code",
    [
        ('arm_controller = "ros"', "stack.arm_controller", "range"),
        ('arm_controller = "Anyspeedup"', "stack.arm_controller", "range"),
        ("arm_controller = 1", "stack.arm_controller", "type"),
        ('arm_preset = "S2"', "stack.arm_preset", "needs_shaped_profile"),
        ('arm_preset = "s1"', "stack.arm_preset", "range"),
        ('arm_preset = "S3"', "stack.arm_preset", "range"),
        ("arm_preset = 1", "stack.arm_preset", "type"),
        ('anyspeedup_root = "relative/dir"', "stack.anyspeedup_root", "not_absolute"),
        ('anyspeedup_root = "/opt/any speed"', "stack.anyspeedup_root", "unsafe"),
        ("anyspeedup_root = 5", "stack.anyspeedup_root", "type"),
    ],
)
def test_a_bad_arm_value_is_named_by_field_and_code(tmp_path, line, field, code):
    # (checked whichever controller is chosen: flipping the one word cannot reveal a typo)
    loaded = read_file(tmp_path, with_arm(line + "\n"))
    assert loaded.state == "invalid" and (field, code) in codes(loaded)
    assert cs.view(loaded)["stack_enabled"] is False
    assert cs.view(loaded)["arm"] is None


def test_s2_is_refused_for_the_reason_the_engine_has_no_shaped_profile(tmp_path):
    assert cs.ARM_PRESETS_REFUSED == {"S2": "needs_shaped_profile"}
    assert "S2" not in cs.ARM_PRESETS and "S2" not in cs.ARM_PRESET_ALPHA
    # the reason is written where the code is defined, and documented
    source = Path(cs.__file__).read_text(encoding="utf-8")
    assert "shaped profile" in source
    loaded = read_file(
        tmp_path,
        with_arm(
            f'arm_controller = "anyspeedup"\narm_preset = "S2"\nanyspeedup_root = "{ANY_ROOT}"\n'
        ),
    )
    assert loaded.state == "invalid"
    assert codes(loaded) == {("stack.arm_preset", "needs_shaped_profile")}


def test_the_anyspeedup_controller_needs_its_root_even_with_the_stack_off(tmp_path):
    loaded = read_file(tmp_path, with_arm('arm_controller = "anyspeedup"\n'))
    assert loaded.state == "invalid"
    assert ("stack.anyspeedup_root", "missing") in codes(loaded)
    off = (
        'schema = "levi.marvel0.control.v1"\n[stack]\nenabled = false\n'
        'arm_controller = "anyspeedup"\n'
    )
    loaded = read_file(tmp_path, off)
    assert codes(loaded) == {("stack.anyspeedup_root", "missing")}


def test_with_the_lab_controller_a_stored_preset_and_root_are_inert(tmp_path):
    loaded = read_file(
        tmp_path,
        with_arm(
            f'arm_controller = "lab"\narm_preset = "S1"\nanyspeedup_root = "{ANY_ROOT}"\n'
        ),
    )
    assert loaded.state == "ok"
    stack_control = loaded.site.stack
    assert (
        stack_control.arm_preset == "S1" and stack_control.anyspeedup_root == ANY_ROOT
    )
    assert stack_control.arm_preset_in_use is None and stack_control.arm_alpha is None
    assert cs.view(loaded)["arm"]["controller"] == "lab"


def test_the_preset_table_is_the_alphas_the_page_offers_the_phase_clock():
    assert set(cs.ARM_PRESET_ALPHA) == set(cs.ARM_PRESETS)
    assert set(cs.ARM_PRESET_ALPHA.values()) <= set(
        inference_mode.CHUNK_ACCEL_V3_ALPHAS
    )
    assert cs.ARM_PRESET_ALPHA["today"] == inference_mode.CHUNK_ACCEL_V3_ALPHA_DEFAULT


def test_the_preset_table_is_the_anyspeedup_controllers_when_it_is_beside_this_checkout():
    folder = anyspeedup_checkout()
    if folder is None:
        pytest.skip("no anyspeedup checkout beside this one")
    tree = ast.parse((folder / "presets.py").read_text(encoding="utf-8"))
    presets = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and getattr(node.target, "id", "") == "PRESETS"
    )
    offered = {name: presets[name]["setpoint_filter_alpha"] for name in cs.ARM_PRESETS}
    assert offered == cs.ARM_PRESET_ALPHA
    # every other preset of the controller is one this page refuses
    assert set(presets) - set(cs.ARM_PRESETS) == set(cs.ARM_PRESETS_REFUSED)
