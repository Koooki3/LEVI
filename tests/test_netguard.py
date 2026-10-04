"""The test suite's network guard (``tests/conftest.py``,
``tests/netguard/sitecustomize.py``): no test reaches the loopback ports of
the services a person uses on this machine (vLLM 8100, product LEVI
7860/7861, live service 7880/7881, robot and policy servers 5000/8000)."""

import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest
from conftest import PROTECTED_PORTS, netguard

from levi.live import gpumgr


def test_every_protected_port_is_refused_in_this_process(_no_protected_ports):
    guard = _no_protected_ports
    start = len(guard.blocked)
    for port in PROTECTED_PORTS:
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=1)
        with socket.socket() as sock, pytest.raises(ConnectionRefusedError):
            sock.connect(("localhost", port))
        with socket.socket() as sock, pytest.raises(ConnectionRefusedError):
            sock.connect_ex(("127.0.0.1", port))
    # urllib (what gpumgr.healthy uses) goes through the same socket calls.
    assert gpumgr.healthy(8100) is False
    with pytest.raises(OSError):
        urllib.request.urlopen("http://127.0.0.1:7861/api/levi/health", timeout=1)
    blocked = guard.blocked[start:]
    assert {r["port"] for r in blocked} == set(PROTECTED_PORTS)
    # Recorded attempts fail the test unless it is marked: this one was
    # deliberate, so forget them before the fixture looks.
    del guard.blocked[start:]


def test_other_ports_and_hosts_are_untouched():
    guard = netguard.GUARD
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert port not in PROTECTED_PORTS
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
    guard.check(("10.1.2.3", 8100))  # not loopback: not this machine's service
    guard.check("/tmp/some.sock")  # a Unix socket


def test_a_child_python_process_is_guarded_and_logged(tmp_path):
    log = tmp_path / "child.jsonl"
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('127.0.0.1', 8100), timeout=1)\n"
        "except ConnectionRefusedError as e:\n"
        "    print('refused', e)\n"
    )
    env = {**os.environ, netguard.LOG_ENV: str(log)}
    done = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "test network guard" in done.stdout
    (record,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert record["port"] == 8100 and record["pid"] != os.getpid()


def test_a_test_that_reaches_a_protected_port_fails(pytester, monkeypatch):
    """The deliberate case: a test reaching :8100 is reported, not passed,
    and the marker is the only way to allow it (run in a pytest of its own
    with this suite's conftest)."""
    here = Path(__file__).resolve().parent
    pytester.makeconftest((here / "conftest.py").read_text())
    guard_dir = pytester.mkdir("netguard")
    shutil.copy(here / "netguard" / "sitecustomize.py", guard_dir)
    pytester.makepyfile(
        test_reach="""
import pytest
from conftest import netguard
from levi.live import gpumgr

def test_reaches_vllm():
    assert gpumgr.healthy(8100) is False  # refused, so it "passes" by itself

@pytest.mark.allow_service_ports
def test_allowed():
    netguard.GUARD.check(("127.0.0.1", 8100))  # not raised: the guard is off

def test_elsewhere_is_fine():
    netguard.GUARD.check(("127.0.0.1", 18100))
"""
    )
    # This process's guard log is not the inner run's.
    monkeypatch.setenv("LEVI_TEST_NETGUARD_OFF", "1")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-q")
    result.assert_outcomes(passed=3, errors=1)
    result.stdout.fnmatch_lines(["*protected service port*8100*"])
