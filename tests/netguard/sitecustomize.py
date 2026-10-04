"""Test-only network guard: refuse connections to this machine's own
addresses on the ports of services people on it are using.

The test suite (``tests/conftest.py``) installs it in the pytest process and
puts this directory first on ``PYTHONPATH`` so every Python process a test
starts imports it as ``sitecustomize`` too. It does nothing unless
``LEVI_TEST_NETGUARD_PORTS`` is set (and ``LEVI_TEST_NETGUARD_OFF`` is not),
so it never affects the product.

"This machine" is any loopback or unspecified address (IPv4, IPv6, and IPv4
mapped into IPv6), the names ``localhost`` and ``ip6-localhost``, this host's
name and every address it resolves to. A refused ``connect`` or
``create_connection`` raises ``ConnectionRefusedError`` and a refused
``connect_ex`` returns ``errno.ECONNREFUSED``, as a closed port would. Each
attempt is recorded: in the pytest process in ``GUARD.blocked``, in a child
process as one JSON line (with its pid and argv) appended to
``LEVI_TEST_NETGUARD_LOG``.
"""

import errno
import ipaddress
import json
import os
import socket
import sys
import threading

PORTS_ENV = "LEVI_TEST_NETGUARD_PORTS"
LOG_ENV = "LEVI_TEST_NETGUARD_LOG"
OFF_ENV = "LEVI_TEST_NETGUARD_OFF"
LOCAL_NAMES = {
    "",
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
}

_open = open  # a test may replace builtins.open


def _ip(host):
    try:
        ip = ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def local_addresses():
    """This host's name and the addresses it resolves to (best effort)."""
    names, addresses = set(), set()
    try:
        name = socket.gethostname()
        names.update({name.lower(), name.lower().split(".", 1)[0]})
        for info in socket.getaddrinfo(name, None):
            ip = _ip(info[4][0])
            if ip is not None:
                addresses.add(ip)
    except (OSError, UnicodeError):
        pass
    return names, addresses


class Guard:
    def __init__(self, ports, log=None):
        self.ports = frozenset(int(p) for p in ports)
        self.log = log
        self.enabled = True
        self.blocked = []
        self._lock = threading.Lock()
        self.names, self.addresses = local_addresses()
        # The real socket calls, set by install(); a test replaces them with
        # stubs to prove a protected address never reaches them.
        self._connect = self._connect_ex = self._create_connection = None

    def protected(self, address) -> bool:
        if not isinstance(address, tuple) or len(address) < 2:
            return False  # a Unix socket
        host, port = address[0], address[1]
        if isinstance(host, bytes):
            host = host.decode(errors="replace")
        if not isinstance(port, int) or port not in self.ports:
            return False
        host = str(host).lower()
        if host in LOCAL_NAMES or host in self.names:
            return True
        ip = _ip(host)
        if ip is None:
            return False
        return ip.is_loopback or ip.is_unspecified or ip in self.addresses

    def refuses(self, address) -> bool:
        """Record and say whether a connection to ``address`` is refused."""
        if not self.enabled or not self.protected(address):
            return False
        record = {
            "host": str(address[0]),
            "port": address[1],
            "pid": os.getpid(),
            "argv": sys.argv[:4],
        }
        with self._lock:
            self.blocked.append(record)
        if self.log:
            try:
                with _open(self.log, "a", encoding="utf-8") as sink:
                    sink.write(json.dumps(record) + "\n")
            except OSError:
                pass
        return True

    def check(self, address):
        if self.refuses(address):
            raise ConnectionRefusedError(
                errno.ECONNREFUSED,
                f"test network guard: {address[0]}:{address[1]} is a protected "
                "service port",
            )


GUARD = None


def install(ports, log=None):
    """Patch ``socket`` once per process; returns the guard."""
    global GUARD
    if GUARD is not None:
        return GUARD
    guard = Guard(ports, log)
    guard._connect = socket.socket.connect
    guard._connect_ex = socket.socket.connect_ex
    guard._create_connection = socket.create_connection

    def guarded_connect(self, address):
        guard.check(address)
        return guard._connect(self, address)

    def guarded_connect_ex(self, address):
        if guard.refuses(address):
            return errno.ECONNREFUSED
        return guard._connect_ex(self, address)

    def guarded_create_connection(address, *args, **kwargs):
        guard.check(address)
        return guard._create_connection(address, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.create_connection = guarded_create_connection
    GUARD = guard
    return guard


if os.environ.get(PORTS_ENV) and not os.environ.get(OFF_ENV):
    install(
        [p for p in os.environ[PORTS_ENV].split(",") if p.strip()],
        os.environ.get(LOG_ENV) or None,
    )
