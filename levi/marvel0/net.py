"""The kernel's socket tables, read-only: which sockets listen on, or are
connected to, a port.

Only ``/proc/net/tcp`` and ``/proc/net/tcp6`` are read (and, when
``tcp6`` is missing, whether ``/proc/net/if_inet6`` exists) (``proc_net`` is a fake
folder in tests). Nothing connects to a port, nothing lists ``/proc``, and no
``/proc/<pid>`` is opened: who owns a socket is decided elsewhere, from our own
units. A table that cannot be read makes the whole answer not ``ok``: the
callers never take an unreadable table for a free port.
"""

import ipaddress
from dataclasses import dataclass
from pathlib import Path

PROC_NET = Path("/proc/net")
TABLES = ("tcp", "tcp6")
LISTEN = "0A"
ESTABLISHED = "01"


@dataclass(frozen=True)
class Sock:
    proto: str  # "tcp" or "tcp6"
    local_port: int
    remote_port: int
    state: str  # the kernel's hex state: "0A" LISTEN, "01" ESTABLISHED
    inode: int
    local_addr: str = ""  # the address in text form; "" when it cannot be read
    remote_addr: str = ""


@dataclass(frozen=True)
class Table:
    ok: bool
    socks: tuple
    problem: str | None = None


def _address(field) -> str:
    """The address of an ``address:port`` field as text, or ``""``. The kernel
    writes each 32-bit word of the address in host (little-endian) order: one
    word for IPv4, four for IPv6."""
    try:
        raw = bytes.fromhex(field.rsplit(":", 1)[0])
    except ValueError:
        return ""
    if len(raw) not in (4, 16):
        return ""
    packed = b"".join(raw[i : i + 4][::-1] for i in range(0, len(raw), 4))
    return str(ipaddress.ip_address(packed))


def _rows(proto, text) -> tuple:
    """``(socks, unparsable)`` of one table. The header line and blank lines
    are not rows; every other line that has too few fields, or whose port,
    state, inode or either address does not parse, is counted in
    ``unparsable`` (``read`` then refuses the whole table: a row it cannot
    read may be the very listener or client asked about). ``_loopback``
    still takes ``""`` for a local address, as a second line of defence."""
    out, unparsable = [], 0
    for line in text.splitlines()[1:]:
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) < 10:
            unparsable += 1
            continue
        try:
            local = int(fields[1].rsplit(":", 1)[1], 16)
            remote = int(fields[2].rsplit(":", 1)[1], 16)
            int(fields[3], 16)
            inode = int(fields[9])
        except (IndexError, ValueError):
            unparsable += 1
            continue
        local_addr, remote_addr = _address(fields[1]), _address(fields[2])
        if not local_addr or not remote_addr:
            # An address that does not parse is a line that does not parse.
            unparsable += 1
            continue
        out.append(
            Sock(
                proto,
                local,
                remote,
                fields[3].upper(),
                inode,
                local_addr,
                remote_addr,
            )
        )
    return out, unparsable


def _no_ipv6(base) -> bool:
    """A missing ``tcp6`` means "no IPv6 sockets" only on a host without
    IPv6: ``if_inet6`` must be missing too (ENOENT). If it exists, or cannot
    be examined, a missing ``tcp6`` says nothing."""
    try:
        (base / "if_inet6").lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def read(proc_net=None) -> Table:
    """Both tables. ``tcp`` unreadable or missing makes the answer not
    ``ok``; so does ``tcp6`` unless it is missing (ENOENT) on a host without
    IPv6 (``if_inet6`` missing as well, ``_no_ipv6``). A permission error,
    any other OSError, text that cannot be decoded or a single unparsable
    line (``_rows``) is never taken for an empty or partial table."""
    base = PROC_NET if proc_net is None else Path(proc_net)
    socks = []
    for name in TABLES:
        try:
            text = (base / name).read_text()
        except FileNotFoundError:
            if name == "tcp":
                return Table(False, (), "the tcp socket table is missing")
            if _no_ipv6(base):
                continue
            return Table(
                False, (), "the tcp6 socket table is missing on a host with IPv6"
            )
        except (OSError, UnicodeDecodeError):
            return Table(False, (), f"the {name} socket table could not be read")
        rows, unparsable = _rows(name, text)
        if unparsable:
            return Table(False, (), f"{unparsable} unparsable lines in {name}")
        socks.extend(rows)
    return Table(True, tuple(socks))


def listeners(table, port) -> list:
    """The sockets listening on ``port`` (IPv4 and IPv6)."""
    return [s for s in table.socks if s.state == LISTEN and s.local_port == port]


def _loopback(address) -> bool:
    """Whether ``address`` is a loopback one (``127.0.0.0/8``, ``::1``, or
    either mapped into IPv6). An address that could not be read (``""``) is
    taken as one: the kernel never writes such a row, and a client that might
    be ours must not go unseen."""
    if not address:
        return True
    ip = ipaddress.ip_address(address)
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def established(table, port) -> list:
    """The connected sockets that are clients of the server on ``port``: a
    socket the server accepted (``port`` is its local port, whoever the peer
    is), or a socket of this host connected to ``port`` on a loopback address
    (the client's own end of a local client, whichever way round the table
    lists the two ends). An outgoing connection of this host to ``port`` of a
    remote machine is some other service, not a client of the policy server."""
    return [
        s
        for s in table.socks
        if s.state == ESTABLISHED
        and (
            s.local_port == port or (s.remote_port == port and _loopback(s.remote_addr))
        )
    ]
