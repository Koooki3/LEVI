"""RIG-01: the port, GPU and GPU-lock probes open /proc/<pid> only for our own
processes (the pids in our levi-* systemd --user units), never another
process's, and never list /proc itself. A mechanical check: every path the
three probes open, stat or list is recorded and compared with our pids."""

import builtins
import os
from pathlib import Path

import pytest
from test_setup_probes import Clock, FakeProc, nvidia, probe

from levi.setup import probes as P


@pytest.fixture
def proc(tmp_path):
    return FakeProc(tmp_path / "proc")


def test_own_pids_are_the_levi_units_and_nothing_else(tmp_path):
    root = tmp_path / "cg" / "user.slice" / "user-1000.slice" / "user@1000.service"
    for rel, pids in (
        ("app.slice/levi-product.service", "10\n11\n"),
        ("app.slice/levi-aeri-vlm-x.service/sub", "12\n"),  # a sub-cgroup counts
        ("levi-heavy.scope", "13\n"),  # directly under the user manager
        ("app.slice/somebody-else.service", "99\n"),
        ("app.slice/levi-not-a-unit", "98\n"),  # neither .service nor .scope
    ):
        (root / rel).mkdir(parents=True)
        (root / rel / "cgroup.procs").write_text(pids)
    assert P.own_pids(tmp_path / "cg", 1000) == {10, 11, 12, 13}
    assert P.own_pids(tmp_path / "missing", 1000) == set()  # unreadable: nobody is ours


def _record(monkeypatch):
    seen = []

    def wrap(fn):
        def inner(path, *a, **k):
            seen.append(os.fspath(path) if not isinstance(path, int) else "")
            return fn(path, *a, **k)

        return inner

    for name in ("open", "listdir", "scandir", "readlink", "lstat", "stat"):
        monkeypatch.setattr(os, name, wrap(getattr(os, name)))
    monkeypatch.setattr(builtins, "open", wrap(builtins.open))
    for name in ("read_text", "read_bytes", "iterdir"):
        real = getattr(Path, name)
        monkeypatch.setattr(
            Path,
            name,
            lambda self, *a, _r=real, **k: seen.append(str(self)) or _r(self, *a, **k),
        )
    return seen


def test_three_probes_open_no_other_process(proc, tmp_path, monkeypatch):
    lock = tmp_path / "gpu.lock"
    lock.write_text("")
    st = os.stat(lock)
    key = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    proc.listen(7861, "111")
    proc.listen(8000, "333")
    proc.process(50, "python", sockets=["111"])  # ours
    proc.process(51, "worker", ppid=50)  # ours, on the GPU
    proc.process(70, "theirs", sockets=["333"])  # someone else's socket
    proc.process(80, "isaac", ppid=70)  # someone else's GPU process
    proc.process(90, "flock")  # someone else's lock holder
    proc.own(50, 51)
    (proc.root / "locks").write_text(
        f"1: FLOCK  ADVISORY  WRITE 90 {key} 0 EOF\n2: FLOCK  ADVISORY  WRITE 51 {key} 0 EOF\n"
    )
    ctx = proc.ctx(gpu_lock_files=(("levi", str(lock)),))
    p = probe(
        runner=nvidia(apps=["51, python3, 9000", "80, isaac-sim, 12000"]), clock=Clock()
    )
    seen = _record(monkeypatch)
    ports, gpu, locks = p._ports(ctx), p._gpu(ctx), p._locks(ctx)
    monkeypatch.undo()

    root = str(proc.root)
    per_pid = set()
    for path in seen:
        if path.rstrip("/") == root:
            pytest.fail(f"/proc itself was opened or listed: {path}")
        if path.startswith(root + "/"):
            head = path[len(root) + 1 :].split("/", 1)[0]
            if head.isdigit():
                per_pid.add(int(head))
    assert per_pid <= {50, 51}, (
        f"another process's /proc/<pid> was opened: {per_pid - {50, 51}}"
    )
    assert 50 in per_pid  # the owners were in fact looked for

    rows = {r["port"]: r for r in ports["ports"]}
    assert rows[7861]["pid"] == 50 and rows[8000]["pid"] is None
    assert [a["pid"] for a in gpu["processes"]] == [51, None]
    holders = {h["pid"]: h["process"] for h in locks["locks"][0]["holders"]}
    assert holders == {90: None, 51: "worker"}  # someone else's holder: no name
