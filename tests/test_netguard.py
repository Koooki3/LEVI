"""The test suite's network guard (``tests/conftest.py``,
``tests/netguard/sitecustomize.py``): no test reaches the ports of the
services a person uses on this machine (vLLM 8100, product LEVI 7860/7861,
live service 7880/7881, robot and policy servers 5000/8000).

None of these tests may ever really connect to one of those ports, even if
the guard were broken: the real socket calls behind the guard are replaced
by stubs that fail the test before anything is sent."""

import errno
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import PROTECTED_PORTS, netguard


@pytest.fixture
def no_real_connection(monkeypatch):
    """The guard with its real socket calls replaced by failing stubs."""
    guard = netguard.GUARD

    def boom(*args, **kwargs):
        pytest.fail(f"a protected address got past the guard: {args[-1:]}")

    monkeypatch.setattr(guard, "_connect", boom)
    monkeypatch.setattr(guard, "_connect_ex", boom)
    monkeypatch.setattr(guard, "_create_connection", boom)
    return guard


def forget(guard, start):
    """Deliberate attempts: drop them before the autouse fixture looks."""
    attempts = guard.blocked[start:]
    del guard.blocked[start:]
    return attempts


def this_hosts_addresses():
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(info[4][0])
    except OSError:
        pass
    return sorted(found)


HOSTS = [
    "127.0.0.1",
    "127.1.2.3",
    "localhost",
    "LOCALHOST",
    "ip6-localhost",
    "::1",
    "0:0:0:0:0:0:0:1",
    "::ffff:127.0.0.1",
    "0.0.0.0",
    "::",
    "",
    socket.gethostname(),
    *this_hosts_addresses(),
]


@pytest.mark.parametrize("host", HOSTS)
def test_every_address_of_this_machine_is_protected(host):
    guard = netguard.GUARD
    start = len(guard.blocked)
    for port in PROTECTED_PORTS:
        assert guard.protected((host, port)), (host, port)
        assert guard.protected((host, port, 0, 0)), (host, port)  # IPv6 tuple
        with pytest.raises(ConnectionRefusedError):
            guard.check((host, port))
    assert not guard.protected((host, 18100))
    assert len(forget(guard, start)) == len(PROTECTED_PORTS)


def test_other_hosts_and_unix_sockets_are_not_protected():
    guard = netguard.GUARD
    assert not guard.protected(("192.0.2.1", 8100))  # TEST-NET-1, not ours
    assert not guard.protected(("example.invalid", 8100))
    assert not guard.protected("/tmp/some.sock")
    assert not guard.protected((b"\x00abstract", 8100))


def test_every_socket_call_is_refused_before_it_connects(no_real_connection):
    guard = no_real_connection
    start = len(guard.blocked)
    for port in PROTECTED_PORTS:
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=1)
        with socket.socket() as sock, pytest.raises(ConnectionRefusedError):
            sock.connect(("localhost", port))
        with (
            socket.socket(socket.AF_INET6) as sock,
            pytest.raises(ConnectionRefusedError),
        ):
            sock.connect(("::ffff:127.0.0.1", port))
        with socket.socket() as sock:
            # As a closed port answers: an error number, not an exception.
            assert sock.connect_ex(("127.0.0.1", port)) == errno.ECONNREFUSED
    attempts = forget(guard, start)
    assert len(attempts) == 4 * len(PROTECTED_PORTS)
    assert {a["port"] for a in attempts} == set(PROTECTED_PORTS)


def test_other_ports_go_through():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        port = server.getsockname()[1]
        assert port not in PROTECTED_PORTS
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
        with socket.socket() as sock:
            assert sock.connect_ex(("127.0.0.1", port)) == 0


def test_a_child_python_process_is_guarded_and_logged(tmp_path):
    log = tmp_path / "child.jsonl"
    code = (
        "import socket, sys\n"
        "import sitecustomize as g\n"
        "if g.GUARD is None:\n"
        "    sys.exit('the guard is not installed: not connecting')\n"
        "def boom(*a, **k):\n"
        "    sys.exit('a protected address got past the guard')\n"
        "g.GUARD._connect = g.GUARD._connect_ex = g.GUARD._create_connection = boom\n"
        "try:\n"
        "    socket.create_connection(('127.0.0.1', 8100), timeout=1)\n"
        "except ConnectionRefusedError as e:\n"
        "    print('refused', e)\n"
    )
    env = {**os.environ, netguard.LOG_ENV: str(log)}
    env.pop(netguard.OFF_ENV, None)
    done = subprocess.run(
        [sys.executable, "-c", code, "marker-arg"],
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
    assert record["argv"] == ["-c", "marker-arg"]


def test_a_test_that_reaches_a_protected_port_fails(pytester, monkeypatch):
    """The deliberate case, in a pytest of its own with this suite's conftest:
    a test reaching :8100 is reported (with the child's pid and argv when a
    child tried), and the marker is the only way to allow it."""
    here = Path(__file__).resolve().parent
    pytester.makeconftest((here / "conftest.py").read_text())
    guard_dir = pytester.mkdir("netguard")
    shutil.copy(here / "netguard" / "sitecustomize.py", guard_dir)
    pytester.makepyfile(
        test_reach="""
import os
import socket
import subprocess
import sys

import pytest
from conftest import netguard

def boom(*args, **kwargs):
    raise SystemExit("a protected address got past the guard")

@pytest.fixture(autouse=True)
def stubbed(monkeypatch):
    for name in ("_connect", "_connect_ex", "_create_connection"):
        monkeypatch.setattr(netguard.GUARD, name, boom)

def test_reaches_vllm():
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", 8100), timeout=1)

CHILD = (
    "import socket, sys, sitecustomize as g\\n"
    "assert g.GUARD is not None\\n"
    "def boom(*a, **k): sys.exit(9)\\n"
    "g.GUARD._connect = g.GUARD._connect_ex = g.GUARD._create_connection = boom\\n"
    "s = socket.socket()\\n"
    "print(s.connect_ex(('127.0.0.1', 7861)))\\n"
)

def test_a_child_reaches_the_product():
    out = subprocess.run([sys.executable, "-c", CHILD, "child-arg"],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "111"

@pytest.mark.allow_service_ports
def test_allowed():
    assert os.environ[netguard.OFF_ENV] == "1"  # children unguarded too
    assert not netguard.GUARD.refuses(("127.0.0.1", 8100))  # guard is off

def test_the_switch_is_back_after_an_allowed_test():
    assert netguard.OFF_ENV not in os.environ

def test_elsewhere_is_fine():
    assert not netguard.GUARD.refuses(("127.0.0.1", 18100))
"""
    )
    # The inner run's guard layer is its conftest's; this process's
    # sitecustomize layer and log must stay out of it.
    monkeypatch.delenv(netguard.OFF_ENV, raising=False)
    monkeypatch.setenv(netguard.PORTS_ENV, "")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-q")
    result.assert_outcomes(passed=5, errors=2)
    result.stdout.fnmatch_lines(
        [
            "*protected service port*",
            "*127.0.0.1:8100 from pid*",
            "*127.0.0.1:7861 from pid * argv ['-c', 'child-arg']*",
        ]
    )
