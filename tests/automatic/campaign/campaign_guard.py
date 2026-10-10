"""Guards for the campaign tests (also installed by their child processes).

- Sockets: any ``connect`` is refused and recorded. The campaign code never
  opens a connection (its readiness check reads ``/proc``); an attempt on a
  robot or policy port (5000, 5001, 5100, 7470, 8000) fails the test.
- Processes: starting ``systemctl``, ``systemd-run`` or ``nvidia-smi`` is
  refused and recorded: no test starts or stops a real unit.
"""

import os
import socket
import subprocess

import pytest

ROBOT_PORTS = (5000, 5001, 5100, 7470, 8000)
UNIT_TOOLS = ("systemctl", "systemd-run", "nvidia-smi")


class Guard:
    def __init__(self):
        self.connects: list = []
        self.commands: list = []
        self._saved = {}

    @staticmethod
    def _port(address):
        if isinstance(address, tuple) and len(address) >= 2:
            return address[1]
        return None

    def install(self):
        guard = self
        real_connect = socket.socket.connect
        real_connect_ex = socket.socket.connect_ex
        real_create = socket.create_connection
        real_popen_init = subprocess.Popen.__init__

        def connect(sock, address):
            guard.connects.append((address, os.getpid()))
            raise ConnectionRefusedError(f"campaign test guard: connect to {address}")

        def connect_ex(sock, address):
            guard.connects.append((address, os.getpid()))
            return 111

        def create_connection(address, *args, **kwargs):
            guard.connects.append((address, os.getpid()))
            raise ConnectionRefusedError(f"campaign test guard: connect to {address}")

        def popen_init(proc, args, *rest, **kwargs):
            argv = [args] if isinstance(args, (str, bytes)) else list(args)
            first = os.path.basename(str(argv[0]).split()[0]) if argv else ""
            if first in UNIT_TOOLS:
                guard.commands.append(argv)
                raise PermissionError(
                    f"campaign test guard: {first} is not run in tests"
                )
            return real_popen_init(proc, args, *rest, **kwargs)

        self._saved = {
            "connect": real_connect,
            "connect_ex": real_connect_ex,
            "create": real_create,
            "popen": real_popen_init,
        }
        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        socket.create_connection = create_connection
        subprocess.Popen.__init__ = popen_init
        return self

    def uninstall(self):
        if not self._saved:
            return
        socket.socket.connect = self._saved["connect"]
        socket.socket.connect_ex = self._saved["connect_ex"]
        socket.create_connection = self._saved["create"]
        subprocess.Popen.__init__ = self._saved["popen"]
        self._saved = {}

    def robot_connects(self) -> list:
        return [a for a, _ in self.connects if self._port(a) in ROBOT_PORTS]


# --- the fixtures (imported by every campaign test module) -------------------------
#
# Not a conftest.py: the test folders have no __init__.py, so a second
# ``conftest`` module would shadow tests/conftest.py for the modules that
# ``from conftest import ...``.


@pytest.fixture(autouse=True, name="aeri_home")
def aeri_home_fixture(tmp_path, monkeypatch):
    """A private LEVI_AERI_HOME: no test writes the real ~/.levi-aeri."""
    home = tmp_path / "aeri-home"
    monkeypatch.setenv("LEVI_AERI_HOME", str(home))
    return home


@pytest.fixture(autouse=True, name="campaign_guard")
def guard_fixture():
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
