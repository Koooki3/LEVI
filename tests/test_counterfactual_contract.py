"""The action contract and its validator (levi/counterfactual)."""

import math
import re
from pathlib import Path

import numpy as np
import pytest

from levi.counterfactual import (
    UnknownContract,
    get,
    ids,
    register,
    validate_action_block,
)
from levi.counterfactual import validation as v

PROJECT = Path(__file__).resolve().parents[1]
WORKSPACE = PROJECT.parent  # the directory that holds LEVI/ and its siblings


def _block(h=10):
    """A plausible absolute block: a slow drift, gripper open then closed."""
    t = np.arange(h, dtype=np.float32)[:, None]
    steps = np.hstack([0.5 + 0.01 * t, 0.0 * t, 0.3 + 0.0 * t,
                       3.1 + 0.0 * t, 0.0 * t, 0.0 * t,
                       (t < 6).astype(np.float32)])  # fmt: skip
    return steps.astype(np.float32)


META = {"representation": "absolute", "time_scale": 1.0, "action_mask": [1] * 10}


def _status(result, name):
    return next(c.status for c in result.checks if c.name == name)


def test_a_good_block_passes_with_every_check_listed():
    result = validate_action_block(_block(), "fr3-robotiq@1", META)
    assert result.passed and not result.failures and not result.warnings
    assert result.contract_id == "fr3-robotiq" and result.contract_version == 1
    assert {c.name for c in result.checks} >= {
        "contract",
        "array",
        "shape",
        "finite",
        "gripper_values",
        "representation",
        "time_scale",
        "action_mask",
        "value_ranges",
    }
    assert "does not show that the motion is physically feasible" in result.limits
    assert result.to_dict()["passed"] is True
    # Unpinned id means the newest version.
    assert validate_action_block(_block(), "fr3-robotiq", META).passed


@pytest.mark.parametrize("shape", [(9, 7), (10, 6), (10, 8), (7,), (1, 10, 7)])
def test_a_wrong_shape_fails_and_later_checks_say_why_they_did_not_run(shape):
    result = validate_action_block(np.zeros(shape, np.float32), "fr3-robotiq", META)
    assert not result.passed
    assert _status(result, "shape") == "fail"
    assert _status(result, "finite") == "skipped"
    assert _status(result, "gripper_values") == "skipped"


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_non_finite_values_fail(bad):
    block = _block()
    block[3, 1] = bad
    result = validate_action_block(block, "fr3-robotiq", META)
    assert not result.passed and _status(result, "finite") == "fail"
    assert _status(result, "gripper_values") == "skipped"


@pytest.mark.parametrize("value", [0.5, -1.0, 2.0, 0.999])
def test_a_gripper_value_outside_the_contract_fails(value):
    block = _block()
    block[2, 6] = value
    result = validate_action_block(block, "fr3-robotiq", META)
    assert not result.passed and _status(result, "gripper_values") == "fail"


def test_not_an_array_or_not_numeric_is_refused_without_looking_inside():
    for thing in (
        [[0.0] * 7] * 10,
        "x",
        None,
        np.array([["a"] * 7] * 10),
        np.zeros((10, 7), bool),
    ):
        result = validate_action_block(thing, "fr3-robotiq", META)
        assert not result.passed and _status(result, "array") == "fail"


def test_the_declared_representation_must_match_the_contract():
    for declared in ("delta", None, "relative"):
        meta = {**META, "representation": declared}
        if declared is None:
            meta.pop("representation")
        result = validate_action_block(_block(), "fr3-robotiq", meta)
        assert not result.passed and _status(result, "representation") == "fail"


@pytest.mark.parametrize(
    "mask",
    [
        None,
        [0] * 10,
        [False] * 10,
        [1] * 9,
        [1] * 11,
        [[1] * 10],
        [2] * 10,
        "1111111111",
        [0.5] * 10,
    ],
)
def test_a_mask_that_is_missing_empty_or_the_wrong_length_fails(mask):
    meta = {**META}
    if mask is None:
        meta.pop("action_mask")
    else:
        meta["action_mask"] = mask
    result = validate_action_block(_block(), "fr3-robotiq", meta)
    assert not result.passed and _status(result, "action_mask") == "fail"


def test_a_mask_with_one_valid_step_or_booleans_passes():
    assert validate_action_block(
        _block(), "fr3-robotiq", {**META, "action_mask": [0] * 9 + [1]}
    ).passed
    assert validate_action_block(
        _block(),
        "fr3-robotiq",
        {**META, "action_mask": np.array([True] * 4 + [False] * 6)},
    ).passed


def test_time_scale_is_recorded_and_a_large_deviation_warns_without_failing():
    near = validate_action_block(_block(), "fr3-robotiq", {**META, "time_scale": 0.95})
    assert near.passed and not near.warnings  # 9.5 Hz rollouts sit on the tolerance
    off = validate_action_block(_block(), "fr3-robotiq", {**META, "time_scale": 0.5})
    assert off.passed and _status(off, "time_scale") == "warning"
    assert "50.0%" in next(c.detail for c in off.checks if c.name == "time_scale")
    missing = {k: x for k, x in META.items() if k != "time_scale"}
    result = validate_action_block(_block(), "fr3-robotiq", missing)
    assert result.passed and _status(result, "time_scale") == "warning"
    for bad in (0, -1.0, float("nan"), float("inf"), True, "1"):
        result = validate_action_block(
            _block(), "fr3-robotiq", {**META, "time_scale": bad}
        )
        assert not result.passed and _status(result, "time_scale") == "fail"


def test_implausible_units_warn_only_for_an_absolute_block():
    block = _block()
    block[0, 0] = 500.0  # millimetres
    block[1, 4] = 90.0  # degrees
    result = validate_action_block(block, "fr3-robotiq", META)
    assert result.passed and _status(result, "value_ranges") == "warning"
    detail = next(c.detail for c in result.checks if c.name == "value_ranges")
    assert "millimetres" in detail and "degrees" in detail


def test_an_unregistered_contract_is_refused():
    for name in ("nope", "fr3-robotiq@2", "fr3-robotiq@x", "", None, "@1"):
        result = validate_action_block(_block(), name, META)
        assert not result.passed and result.contract_version is None
        assert result.checks[0].name == "contract" and result.checks[0].status == "fail"
    with pytest.raises(UnknownContract):
        get("nope")
    assert ids() == ["fr3-robotiq@1"]


def test_a_contract_is_immutable_and_never_replaced():
    contract = get("fr3-robotiq@1")
    with pytest.raises(AttributeError):
        contract.action_horizon = 5  # type: ignore[misc]
    with pytest.raises(ValueError, match="already registered"):
        register(contract)
    with pytest.raises(ValueError, match="gripper_index"):
        register(_variant(contract, id="bad-grip", gripper_index=9))
    assert get("fr3-robotiq@1") is get("fr3-robotiq") is contract
    for bad in (
        {"id": "bad-hz", "control_hz": 0.0},
        {"id": "bad-hz2", "control_hz": -10.0},
        {"id": "bad-hz3", "control_hz": float("inf")},
        {"id": "bad-exec", "executed_horizon": 0},
        {"id": "bad-exec2", "executed_horizon": 11},
    ):
        with pytest.raises(ValueError, match="control_hz|executed_horizon"):
            register(_variant(contract, **bad))
    assert "bad-hz@1" not in ids()


def _variant(contract, **changes):
    import dataclasses

    return dataclasses.replace(contract, **changes)


def test_a_second_registered_contract_is_validated_by_its_own_numbers():
    base = get("fr3-robotiq@1")
    small = register(
        _variant(base, id="test-arm-small", action_horizon=4, executed_horizon=4)
    )
    try:
        ok = validate_action_block(
            _block(4), small.ref, {**META, "action_mask": [1] * 4}
        )
        assert ok.passed
        assert not validate_action_block(_block(10), small.ref, META).passed
    finally:
        from levi.counterfactual import schema

        schema._REGISTRY.pop((small.id, small.version))


def test_the_validator_never_opens_a_file(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the validator touched the file system")

    monkeypatch.setattr("builtins.open", refuse)
    monkeypatch.setattr(Path, "read_bytes", refuse)
    assert validate_action_block(_block(), "fr3-robotiq", META).passed
    assert "pickle" not in "".join(
        line
        for line in Path(v.__file__).read_text().splitlines()
        if line.startswith("import")
    )


# ---------------------------------------------------------- the constants


def test_contract_constants_are_the_ones_in_the_code_they_were_read_from():
    c = get("fr3-robotiq@1")
    assert c.dimension_names == ("x", "y", "z", "rx", "ry", "rz", "gripper")
    assert (c.action_horizon, c.executed_horizon, c.control_hz) == (10, 8, 10.0)
    assert math.isclose(c.control_period_s, 0.1)
    assert c.representation == "absolute" and c.action_semantics == "next_state"
    assert c.frame == "franka_hand_tcp" and c.gripper_index == 6
    assert c.gripper_values == (0.0, 1.0) and c.gripper_open_value == 1.0
    assert c.rebase_euler_index == 3 and math.isclose(c.rebase_euler_offset, math.pi)
    assert (c.position_unit, c.rotation_unit) == ("m", "rad")


def _lines(path: Path, first: int, last: int) -> str:
    return "\n".join(path.read_text().splitlines()[first - 1 : last])


def test_the_conversion_in_this_repository_still_writes_what_the_contract_says():
    """Strict: these files are in the repository, so line and text must match."""
    c = get("fr3-robotiq@1")
    refs = [s for s in c.source if s.in_repository]
    assert len(refs) == 3
    for ref in refs:
        assert ref.snippet in _lines(PROJECT / ref.path, ref.first, ref.last), ref
    pipeline = (PROJECT / "levi/conversion/pipeline.py").read_text()
    assert "np.column_stack([xyz, orientation, command])" in pipeline
    assert "np.vstack([state[1:], state[-1:]])" in pipeline  # action[t] = state[t+1]
    options = (PROJECT / "levi/conversion/options.py").read_text()
    assert re.search(r"fps: float = Field\(10,", options)
    assert re.search(
        r'orientation: Literal\["euler", "quaternion"\] = "euler"', options
    )
    raw = (PROJECT / "levi/conversion/raw.py").read_text()
    assert '(command == "open").to_numpy(float)' in raw  # open is 1.0
    names = (PROJECT / "levi/conversion/pipeline.py").read_text()
    assert 'return ["x", "y", "z"] + rotation + ["gripper"]' in names


def _outside(relative: str) -> Path:
    path = WORKSPACE / relative
    if not path.is_file():
        pytest.skip(f"{relative} is not next to this checkout")
    return path


def test_the_training_side_files_still_carry_the_contract_values():
    """The files beside this checkout (the maintainer's workspace). Text must
    be present; the line numbers in ``source`` are as of writing and may move."""
    c = get("fr3-robotiq@1")
    for ref in c.source:
        if not ref.in_repository:
            assert ref.snippet in _outside(ref.path).read_text(), ref
    raw = _outside("data_collection_robotiq/scripts/raw2lerobot.py").read_text()
    assert '0.0 if str(command).strip().lower() == "close" else 1.0' in raw
    assert (
        "action[t] = state[t+1]" in raw
        and "action_state = state_rows[index + 1][1]" in raw
    )
    config = _outside("openpi/src/openpi/training/config.py").read_text()
    block = config[config.index('name="pi05_fr3_all_state",') :]
    block = block[: block.index("TrainConfig(")]
    assert "action_horizon=10" in block and "rebase_euler_dim=3" in block
    assert "rebase_euler_offset=np.pi" in block and "wrap_delta_angles=True" in block
    assert (
        '"gripper_convention": "1.0=open, 0.0=closed, commanded not measured"' in block
    )
    assert '"ee_frame": "franka_hand_tcp"' in block
    transforms = _outside("openpi/src/openpi/transforms.py").read_text()
    assert "(np.asarray(values) + np.pi) % (2 * np.pi) - np.pi" in transforms
    client = _outside("openpi/examples/fr3_local/run_robotiq_client.py").read_text()
    assert re.search(r"open_loop_horizon: int = 8\b", client)
    assert re.search(r"control_hz: float = 10\.0\b", client)
    info = _outside(
        "data_collection_robotiq/lerobot_fr3_filtered_robotiq_v3/meta/info.json"
    )
    import json

    meta = json.loads(info.read_text())
    assert meta["fps"] == 10
    for key in ("action", "observation.state"):
        assert meta["features"][key]["names"] == list(c.dimension_names)
        assert meta["features"][key]["shape"] == [c.dimension]


def test_a_block_that_jumps_two_pi_in_rx_between_steps_warns():
    block = _block()
    block[5:, 3] -= 2 * math.pi  # the [0, 2 pi) and (-pi, pi] conventions mixed
    result = validate_action_block(block, "fr3-robotiq", META)
    assert result.passed and _status(result, "rx_continuity") == "warning"
    assert (
        _status(validate_action_block(_block(), "fr3-robotiq", META), "rx_continuity")
        == "pass"
    )
    delta = validate_action_block(
        block, "fr3-robotiq", {**META, "representation": "delta"}
    )
    assert _status(delta, "rx_continuity") == "skipped"
