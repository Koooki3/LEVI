"""Test-only network guard: refuse loopback connections to the ports of
services people on this machine are using.

The test suite (``tests/conftest.py``) installs it in the pytest process and
puts this directory first on ``PYTHONPATH`` so every Python process a test
starts imports it as ``sitecustomize`` too. It does nothing unless
``LEVI_TEST_NETGUARD_PORTS`` is set, so it never affects the product.

A refused attempt raises ``ConnectionRefusedError`` (what a closed port
answers) and is recorded: in the pytest process in ``GUARD.blocked``, in a
child process as one JSON line appended to ``LEVI_TEST_NETGUARD_LOG``.
"""

import json
import os
import socket
import sys
import threading

PORTS_ENV = "LEVI_TEST_NETGUARD_PORTS"
LOG_ENV = "LEVI_TEST_NETGUARD_LOG"
LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::", "", "ip6-localhost"}


class Guard:
    def __init__(self, ports, log=None):
        self.ports = frozenset(int(p) for p in ports)
        self.log = log
        self.enabled = True
        self.blocked = []
        self._lock = threading.Lock()

    def check(self, address):
        if not self.enabled or not isinstance(address, tuple) or len(address) < 2:
            return
        host, port = address[0], address[1]
        if isinstance(host, bytes):
            host = host.decode(errors="replace")
        if not isinstance(port, int) or port not in self.ports:
            return
        if str(host).lower() not in LOOPBACK and not str(host).startswith("127."):
            return
        record = {"host": str(host), "port": port, "pid": os.getpid()}
        with self._lock:
            self.blocked.append(record)
        if self.log:
            try:
                with _open(self.log, "a", encoding="utf-8") as sink:
                    sink.write(json.dumps({**record, "argv": sys.argv[:4]}) + "\n")
            except OSError:
                pass
        raise ConnectionRefusedError(
            111, f"test network guard: {host}:{port} is a protected service port"
        )


GUARD = None
_open = open  # a test may replace builtins.open


def install(ports, log=None):
    """Patch ``socket`` once per process; returns the guard."""
    global GUARD
    if GUARD is not None:
        return GUARD
    guard = Guard(ports, log)
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    create_connection = socket.create_connection

    def guarded_connect(self, address):
        guard.check(address)
        return connect(self, address)

    def guarded_connect_ex(self, address):
        guard.check(address)
        return connect_ex(self, address)

    def guarded_create_connection(address, *args, **kwargs):
        guard.check(address)
        return create_connection(address, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.create_connection = guarded_create_connection
    GUARD = guard
    return guard


if os.environ.get(PORTS_ENV) and not os.environ.get("LEVI_TEST_NETGUARD_OFF"):
    install(
        [p for p in os.environ[PORTS_ENV].split(",") if p.strip()],
        os.environ.get(LOG_ENV) or None,
    )
