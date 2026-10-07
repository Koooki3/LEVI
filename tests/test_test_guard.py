"""The suite refuses to run in a checkout whose workspace a running LEVI uses (tests/conftest.py)."""

import importlib.util
import os
from pathlib import Path

import pytest

CONFTEST = Path(__file__).with_name("conftest.py")


def _guard():
    spec = importlib.util.spec_from_file_location(
        "levi_tests_conftest_under_test", CONFTEST
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.pytest_sessionstart


def _workspace(monkeypatch, tmp_path, pid_text):
    from levi import maintenance

    state = tmp_path / "ws/outputs/LEVI/workbench"
    state.mkdir(parents=True)
    (state / "server.pid").write_text(pid_text)
    monkeypatch.setattr(maintenance, "STATE", state)
    monkeypatch.delenv("LEVI_TESTS_IN_PRODUCT", raising=False)


def test_a_live_server_pid_stops_the_run_with_the_way_out(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path, str(os.getpid()))
    with pytest.raises(pytest.exit.Exception) as stop:
        _guard()(None)
    assert "git worktree add" in str(stop.value) and "LEVI_TESTS_IN_PRODUCT" in str(
        stop.value
    )


def test_a_dead_or_unreadable_pid_lets_the_run_start(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path, "999999999")
    assert _guard()(None) is None
    _workspace(monkeypatch, tmp_path / "again", "not a number")
    assert _guard()(None) is None


def test_the_override_lets_the_run_start(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path, str(os.getpid()))
    monkeypatch.setenv("LEVI_TESTS_IN_PRODUCT", "1")
    assert _guard()(None) is None
