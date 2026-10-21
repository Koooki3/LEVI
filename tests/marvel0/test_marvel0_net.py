"""``levi/marvel0/net.py``: the kernel's socket tables, read-only. The tables are
fake files in a temporary folder; nothing here opens a socket or lists a
process folder, and only the two table files are ever opened."""

import builtins
import ipaddress
import os
import socket
from pathlib import Path

import pytest

from levi.marvel0 import net

HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
    "   uid  timeout inode\n"
)
ZERO4 = "00000000:0000"
ZERO6 = "00000000000000000000000000000000:0000"
LOOP4 = "0100007F"
LOOP6 = "00000000000000000000000001000000"


def row4(local, remote, state, inode, n=0):
    return (
        f"   {n}: {local} {remote} {state} 00000000:00000000 00:00000000 "
        f"00000000  1000        0 {inode} 1 0000000000000000 100 0 0 10 0\n"
    )


def tables(tmp_path, tcp=None, tcp6=None):
    """A fake ``/proc/net`` with the given row lists (None: no such file)."""
    folder = tmp_path / "net"
    folder.mkdir(exist_ok=True)
    if tcp is not None:
        (folder / "tcp").write_text(HEADER + "".join(tcp))
    if tcp6 is not None:
        (folder / "tcp6").write_text(HEADER + "".join(tcp6))
    return folder


def hex4(text):
    """An IPv4 address as ``/proc/net/tcp`` writes it: the four bytes of the
    32-bit word in host (little-endian) order."""
    return ipaddress.IPv4Address(text).packed[::-1].hex().upper()


def hex6(text):
    """An IPv6 address as ``/proc/net/tcp6`` writes it: four such words."""
    packed = ipaddress.IPv6Address(text).packed
    return "".join(packed[i : i + 4][::-1].hex() for i in range(0, 16, 4)).upper()


def at4(address, port):
    return f"{hex4(address)}:{port:04X}"


def at6(address, port):
    return f"{hex6(address)}:{port:04X}"


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the socket tables are read, nothing is connected to")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_listeners_are_the_listen_rows_of_the_port_in_both_tables(tmp_path):
    folder = tables(
        tmp_path,
        tcp=[
            row4(f"{ZERO4[:8]}:1F40", ZERO4, "0A", 111),  # 0.0.0.0:8000 LISTEN
            row4(f"{ZERO4[:8]}:1F41", ZERO4, "0A", 112),  # 8001: another port
        ],
        tcp6=[row4(f"{ZERO6[:32]}:1F40", ZERO6, "0A", 222, 1)],  # [::]:8000
    )
    table = net.read(folder)
    assert table.ok and table.problem is None
    found = net.listeners(table, 8000)
    assert sorted((s.proto, s.inode) for s in found) == [("tcp", 111), ("tcp6", 222)]
    assert [s.local_port for s in found] == [8000, 8000]
    assert net.listeners(table, 9) == []


def test_only_the_listen_state_counts_as_a_listener(tmp_path):
    rows = [
        row4(f"{LOOP4}:1F40", f"{LOOP4}:C000", state, 300 + n, n)
        for n, state in enumerate(("01", "06", "08", "02", "0B"))
    ]
    table = net.read(tables(tmp_path, tcp=rows, tcp6=[]))
    assert net.listeners(table, 8000) == []


def test_established_counts_both_ends_of_a_loopback_client(tmp_path):
    server_side = row4(f"{LOOP4}:1F40", f"{LOOP4}:C350", "01", 401, 0)  # local 8000
    client_side = row4(f"{LOOP4}:C350", f"{LOOP4}:1F40", "01", 402, 1)  # remote 8000
    other = row4(f"{LOOP4}:C351", f"{LOOP4}:0050", "01", 403, 2)  # to port 80
    waiting = row4(f"{LOOP4}:C352", f"{LOOP4}:1F40", "06", 404, 3)  # TIME_WAIT
    closing = row4(f"{LOOP4}:C353", f"{LOOP4}:1F40", "08", 405, 4)  # CLOSE_WAIT
    table = net.read(
        tables(
            tmp_path,
            tcp=[server_side, client_side, other, waiting, closing],
            tcp6=[],
        )
    )
    assert sorted(s.inode for s in net.established(table, 8000)) == [401, 402]
    assert net.established(table, 9999) == []


# --- which established sockets are clients of the policy port ------------------
# A client is a socket the server accepted (the policy port is its LOCAL port,
# whoever the peer is) or a client of this host that connects to the loopback
# policy port. An outgoing connection of this host to some remote service that
# also uses port 8000 is not a client of the policy server.

OWN = "192.168.1.5"
PEER = "10.0.0.9"
ELSEWHERE = "203.0.113.7"


def counted(tmp_path, tcp=(), tcp6=(), port=8000):
    table = net.read(tables(tmp_path, tcp=list(tcp), tcp6=list(tcp6)))
    return sorted(s.inode for s in net.established(table, port))


def test_a_connection_accepted_from_a_remote_peer_counts(tmp_path):
    accepted = row4(at4(OWN, 8000), at4(PEER, 51000), "01", 801)
    assert counted(tmp_path, tcp=[accepted]) == [801]


def test_a_local_client_of_the_loopback_port_counts(tmp_path):
    client = row4(at4("127.0.0.1", 50000), at4("127.0.0.1", 8000), "01", 802)
    assert counted(tmp_path, tcp=[client]) == [802]


def test_an_outgoing_connection_to_a_remote_port_8000_does_not_count(tmp_path):
    outgoing = row4(at4(OWN, 50001), at4(ELSEWHERE, 8000), "01", 803)
    assert counted(tmp_path, tcp=[outgoing]) == []


def test_only_the_remote_end_decides_when_the_port_is_the_remote_one(tmp_path):
    rows = [
        row4(at4(OWN, 50001), at4(ELSEWHERE, 8000), "01", 810, 0),  # remote host
        row4(at4(OWN, 50002), at4(OWN, 8000), "01", 811, 1),  # own LAN address
        row4(at4("127.0.0.1", 50003), at4("127.0.0.1", 8000), "01", 812, 2),
        row4(at4("127.0.0.2", 50004), at4("127.0.0.2", 8000), "01", 813, 3),
    ]
    assert counted(tmp_path, tcp=rows) == [812, 813]


def test_a_peer_of_a_listener_on_this_host_counts_by_the_server_side(tmp_path):
    # The same client through the LAN address: the accepted socket has the
    # policy port as its local port and is counted; the client end is not.
    server_side = row4(at4(OWN, 8000), at4(OWN, 50005), "01", 820, 0)
    client_side = row4(at4(OWN, 50005), at4(OWN, 8000), "01", 821, 1)
    assert counted(tmp_path, tcp=[server_side, client_side]) == [820]


def test_ipv6_accepted_and_loopback_clients_count(tmp_path):
    rows = [
        row4(at6("2001:db8::5", 8000), at6("2001:db8::9", 51000), "01", 830, 0),
        row4(at6("::1", 50000), at6("::1", 8000), "01", 831, 1),
        row4(
            at6("::ffff:127.0.0.1", 50001), at6("::ffff:127.0.0.1", 8000), "01", 832, 2
        ),
    ]
    assert counted(tmp_path, tcp6=rows) == [830, 831, 832]


def test_ipv6_outgoing_connections_to_a_remote_port_8000_do_not_count(tmp_path):
    rows = [
        row4(at6("2001:db8::5", 50000), at6("2001:db8::9", 8000), "01", 840, 0),
        row4(
            at6("::ffff:192.168.1.5", 50001),
            at6(f"::ffff:{ELSEWHERE}", 8000),
            "01",
            841,
            1,
        ),
        row4(
            at6("::ffff:192.168.1.5", 50002), at6(f"::ffff:{OWN}", 8000), "01", 842, 2
        ),
    ]
    assert counted(tmp_path, tcp6=rows) == []


def test_the_addresses_are_read_in_the_kernels_byte_order(tmp_path):
    table = net.read(
        tables(
            tmp_path,
            tcp=[row4(at4("10.1.2.3", 8000), at4("192.0.2.77", 51000), "01", 850)],
            tcp6=[row4(at6("2001:db8::1", 8000), at6("::1", 51000), "01", 851)],
        )
    )
    four, six = sorted(table.socks, key=lambda s: s.inode)
    assert (four.local_addr, four.remote_addr) == ("10.1.2.3", "192.0.2.77")
    assert (six.local_addr, six.remote_addr) == ("2001:db8::1", "::1")


@pytest.mark.parametrize(
    "line",
    [
        row4(at4(OWN, 50006), "0100007:1F40", "01", 860, 0),  # remote: odd hex
        row4("XYZ:1F40", at4(PEER, 51000), "01", 861, 1),  # local: not hex
    ],
)
def test_an_unreadable_address_makes_the_whole_table_not_ok(tmp_path, line):
    # The kernel never writes one; if it did, the table is not trusted at all.
    good = row4(at4(OWN, 8000), at4(PEER, 51000), "01", 862, 2)
    table = net.read(tables(tmp_path, tcp=[good, line], tcp6=[]))
    assert not table.ok and table.socks == ()
    assert table.problem == "1 unparsable lines in tcp"
    assert net.established(table, 8000) == []


def test_ipv6_ports_are_read_from_the_last_colon(tmp_path):
    row = row4(f"{LOOP6}:1F40", f"{LOOP6}:C350", "01", 501)  # [::1]:8000 -> [::1]:50000
    table = net.read(tables(tmp_path, tcp=[], tcp6=[row]))
    (sock,) = net.established(table, 8000)
    assert (sock.proto, sock.local_port, sock.remote_port) == ("tcp6", 8000, 50000)


@pytest.mark.parametrize(
    "garbage",
    [
        "this is not a row\n",  # too few fields
        "   9: ZZZZZZZZ:GGGG 00000000:0000 0A 0 0 0 0 0 0 77\n",  # bad port
        "   8: 0100007F:1F40 00000000:0000 0A 0 0 0 0 0 notanumber\n",  # bad inode
        "   7: 0100007F:1F40 00000000:0000 XQ 0 0 0 0 0 0 78\n",  # bad state
    ],
)
def test_one_unparsable_line_makes_the_whole_table_not_ok(tmp_path, garbage):
    folder = tables(tmp_path, tcp=[row4(f"{ZERO4[:8]}:1F40", ZERO4, "0A", 601)])
    with (folder / "tcp").open("a") as handle:
        handle.write(garbage)
    (folder / "tcp6").write_text(HEADER)
    table = net.read(folder)
    assert not table.ok and table.socks == ()
    assert table.problem == "1 unparsable lines in tcp"


def test_garbage_next_to_a_listener_on_5000_is_still_not_ok(tmp_path):
    """The readable LISTEN row on 5000 is not reported as a partial answer."""
    folder = tables(
        tmp_path,
        tcp=[row4(f"{ZERO4[:8]}:1388", ZERO4, "0A", 602), "garbage line\n"],
        tcp6=["   0: also garbage\n"],
    )
    table = net.read(folder)
    assert not table.ok
    assert net.listeners(table, 5000) == []
    assert table.problem == "1 unparsable lines in tcp"


def test_the_header_and_blank_lines_are_not_counted(tmp_path):
    folder = tables(tmp_path, tcp=[row4(f"{ZERO4[:8]}:1F40", ZERO4, "0A", 603)])
    with (folder / "tcp").open("a") as handle:
        handle.write("\n   \n")
    (folder / "tcp6").write_text(HEADER + "\n")
    table = net.read(folder)
    assert table.ok
    assert [s.inode for s in net.listeners(table, 8000)] == [603]


def test_an_unreadable_or_missing_tcp_table_is_not_ok(tmp_path):
    missing = tables(tmp_path, tcp=None, tcp6=[])
    table = net.read(missing)
    assert not table.ok and table.socks == () and table.problem
    folder = tmp_path / "net"
    (folder / "tcp").mkdir()  # reading a directory fails with an OSError
    table = net.read(folder)
    assert not table.ok and table.problem


def test_a_missing_tcp6_table_is_fine_but_an_unreadable_one_is_not(tmp_path):
    folder = tables(tmp_path, tcp=[row4(f"{ZERO4[:8]}:1F40", ZERO4, "0A", 701)])
    assert net.read(folder).ok  # no tcp6 at all: IPv6 is switched off
    (folder / "tcp6").mkdir()
    table = net.read(folder)
    assert not table.ok and table.problem


def test_read_opens_only_the_two_tables_and_lists_nothing(tmp_path, monkeypatch):
    folder = tables(tmp_path, tcp=[], tcp6=[])
    opened = []
    real_open = builtins.open
    real_read_text = Path.read_text

    def spy_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def spy_read_text(self, *args, **kwargs):
        opened.append(str(self))
        return real_read_text(self, *args, **kwargs)

    def no_listing(*args, **kwargs):
        raise AssertionError("no folder is listed")

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(Path, "read_text", spy_read_text)
    monkeypatch.setattr(os, "listdir", no_listing)
    monkeypatch.setattr(os, "scandir", no_listing)
    monkeypatch.setattr(Path, "iterdir", no_listing)
    assert net.read(folder).ok
    assert sorted(set(opened)) == sorted([str(folder / "tcp"), str(folder / "tcp6")])


def test_the_default_location_is_proc_net_and_never_a_process_folder():
    assert net.PROC_NET == Path("/proc/net")
    assert net.TABLES == ("tcp", "tcp6")


@pytest.mark.parametrize(
    "has_tcp6, has_if_inet6, ok",
    [
        (True, True, True),  # an IPv6 host with its table
        (True, False, True),  # the table is there: it is read
        (False, False, True),  # a host without IPv6
        (False, True, False),  # IPv6 is up but its table is missing
    ],
)
def test_tcp6_and_if_inet6_in_all_four_combinations(
    tmp_path, has_tcp6, has_if_inet6, ok
):
    folder = tables(tmp_path, tcp=[], tcp6=[] if has_tcp6 else None)
    if has_if_inet6:
        (folder / "if_inet6").write_text("")
    table = net.read(folder)
    assert table.ok is ok
    if not ok:
        assert table.socks == () and "tcp6" in table.problem


def test_if_inet6_that_cannot_be_examined_makes_a_missing_tcp6_unknown(
    tmp_path, monkeypatch
):
    folder = tables(tmp_path, tcp=[], tcp6=None)
    real = net.Path.lstat

    def lstat(self, *args, **kwargs):
        if self.name == "if_inet6":
            raise PermissionError("denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(net.Path, "lstat", lstat)
    assert net.read(folder).ok is False


def test_a_tcp6_table_that_cannot_be_read_or_decoded_is_unknown(tmp_path):
    folder = tables(tmp_path, tcp=[], tcp6=None)
    (folder / "tcp6").mkdir()  # IsADirectoryError, an OSError that is not ENOENT
    assert net.read(folder).ok is False
    (folder / "tcp6").rmdir()
    (folder / "tcp6").write_bytes(b"\xff\xfe not text\n")
    assert net.read(folder).ok is False
