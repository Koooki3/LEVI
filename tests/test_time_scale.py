"""LEVI_TEST_TIME_SCALE stretches the wait helpers' wall-clock budgets."""

import pytest
from conftest import scaled, time_scale


def test_the_scale_defaults_to_one_and_multiplies_budgets(monkeypatch):
    monkeypatch.delenv("LEVI_TEST_TIME_SCALE", raising=False)
    assert time_scale() == 1 and scaled(30) == 30
    monkeypatch.setenv("LEVI_TEST_TIME_SCALE", "2")
    assert scaled(30) == 60 and scaled(0.5) == 1.0


@pytest.mark.parametrize("value", ["0", "-1", "fast"])
def test_a_scale_that_is_not_a_positive_number_is_an_error(monkeypatch, value):
    monkeypatch.setenv("LEVI_TEST_TIME_SCALE", value)
    with pytest.raises(ValueError):
        scaled(1)
