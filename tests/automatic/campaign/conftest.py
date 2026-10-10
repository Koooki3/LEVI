"""Campaign tests: a private ``LEVI_AERI_HOME``, no socket connection at all
and no real systemd unit (``campaign_guard``)."""

import pytest
from campaign_guard import Guard


@pytest.fixture(autouse=True)
def aeri_home(tmp_path, monkeypatch):
    home = tmp_path / "aeri-home"
    monkeypatch.setenv("LEVI_AERI_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def campaign_guard():
    guard = Guard().install()
    try:
        yield guard
    finally:
        guard.uninstall()
    if guard.robot_connects():
        pytest.fail(
            f"connected to a robot or policy port: {guard.robot_connects()}",
            pytrace=False,
        )
    if guard.commands:
        pytest.fail(f"started a unit tool: {guard.commands}", pytrace=False)
