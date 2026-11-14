"""The test network guard covers every robot-side port of lab/AGENTS.md section 1.

Those ports (robot servers 5000/5001, the tunnel to the second robot 5100, the
learner 7470, the policy servers 8000/8010) have no authentication and can
drive a robot, so no test may connect to them. If a port is missing from
``PROTECTED_PORTS`` in ``tests/conftest.py``, add it there (the list only ever
grows); do not remove it here. The real socket calls behind the guard are
replaced by stubs that fail the test, so nothing here can reach a port."""

import errno
import os
import socket

import pytest
from conftest import PROTECTED_PORTS, netguard

ROBOT_PORTS = (5000, 5001, 5100, 7470, 8000, 8010)


@pytest.fixture
def no_real_connection(monkeypatch):
    guard = netguard.GUARD

    def boom(*args, **kwargs):
        pytest.fail(f"a robot port got past the guard: {args[-1:]}")

    monkeypatch.setattr(guard, "_connect", boom)
    monkeypatch.setattr(guard, "_connect_ex", boom)
    monkeypatch.setattr(guard, "_create_connection", boom)
    return guard


def test_every_robot_port_is_in_the_protected_list():
    missing = sorted(set(ROBOT_PORTS) - set(PROTECTED_PORTS))
    assert not missing, (
        f"tests/conftest.py PROTECTED_PORTS lacks robot ports {missing}: add them"
    )
    assert isinstance(PROTECTED_PORTS, tuple)


def test_the_installed_guard_and_child_processes_cover_them():
    # The guard in this process and the environment child processes inherit.
    assert set(ROBOT_PORTS) <= netguard.GUARD.ports
    assert set(ROBOT_PORTS) <= {
        int(p) for p in os.environ[netguard.PORTS_ENV].split(",")
    }


@pytest.mark.parametrize("port", ROBOT_PORTS)
def test_a_connection_to_a_robot_port_is_refused_before_it_is_made(
    port, no_real_connection
):
    guard = no_real_connection
    start = len(guard.blocked)
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=0.2)
    with socket.socket() as sock, pytest.raises(ConnectionRefusedError):
        sock.connect(("127.0.0.1", port))
    with socket.socket() as sock:
        assert sock.connect_ex(("127.0.0.1", port)) == errno.ECONNREFUSED
    attempts = guard.blocked[start:]
    del guard.blocked[start:]  # deliberate: the autouse fixture must not flag them
    assert [a["port"] for a in attempts] == [port] * 3
