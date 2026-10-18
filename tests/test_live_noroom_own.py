"""The "no room on the GPU" exemption decides ownership only from our own
registry (root AGENTS.md execution boundary; user 2026-10-14 23:47).

A GPU process is ours when its pid is in the ``cgroup.procs`` of one of our
own units (``levi-aeri-policy-*`` for the policy server, ``levi-live`` for
LEVI's model server); anything else is someone else's and nothing more is
read about it. Linking a port to a unit reads only the kernel's socket tables
and the fds of our own pids. A cgroup that cannot be read exempts nothing.

The fake machine below is a fake kernel tree and a fake systemctl; no real
process is looked at.
"""

# ruff: noqa: F811

import builtins
import functools
import os
import pathlib
import time

import pytest
from test_live_gpu import ctl, fresh, live, serve  # noqa: F401
from test_live_yield import POLICY_APP, add_apps, asleep_vllm, rules_on  # noqa: F401

from levi import own_units
from levi.inference import gpu
from levi.live import gpumgr

BUDGET = 8500
UNIT = "levi-aeri-policy-pi05"


class World:
    """``unit(name, pids)`` (``pids=None``: its cgroup cannot be read),
    ``process(pid, sockets)``, ``listen(port, inode)``."""

    def __init__(self, tmp_path):
        self.proc = tmp_path / "kernel"
        (self.proc / "net").mkdir(parents=True)
        self.cgroups = tmp_path / "cgroup"
        self.cgroups.mkdir()
        self.units: dict = {}
        self.rows: list = []
        self.listing_ok = True
        self._write_table()

    def _write_table(self):
        (self.proc / "net" / "tcp").write_text(
            "header\n" + "".join(r + "\n" for r in self.rows)
        )

    def systemctl(self, argv):
        if "list-units" in argv:
            if not self.listing_ok:
                return 1, ""
            # systemctl lists only the unit types asked for (--type=a,b).
            kinds = next(
                (
                    x.split("=", 1)[1].split(",")
                    for x in argv
                    if x.startswith("--type=")
                ),
                None,
            )
            return 0, "".join(
                f"{u} loaded {'active' if g else 'inactive'} x\n"
                for u, (_, g) in self.units.items()
                if kinds is None or u.rsplit(".", 1)[-1] in kinds
            )
        unit = argv[-1]
        if unit not in self.units:
            return 0, ""
        return 0, self.units[unit][1] + "\n"

    def unit(self, name, pids, *, active=True):
        """``name`` without a suffix is a service. An inactive unit has an
        empty ControlGroup (systemctl show answers "" with exit 0)."""
        if not name.endswith((".service", ".scope")):
            name += ".service"
        group = f"/user.slice/{name}" if active else ""
        self.units[name] = (pids, group)
        if active:
            folder = self.cgroups / "user.slice" / name
            folder.mkdir(parents=True, exist_ok=True)
            if pids is not None:
                (folder / "cgroup.procs").write_text("".join(f"{p}\n" for p in pids))

    def children(self, pid, kids):
        """``<pid>/task/<pid>/children`` of one of our recorded processes."""
        task = self.proc / str(pid) / "task" / str(pid)
        task.mkdir(parents=True, exist_ok=True)
        (task / "children").write_text(" ".join(str(k) for k in kids))

    def process(self, pid, sockets=()):
        fd = self.proc / str(pid) / "fd"
        fd.mkdir(parents=True)
        for n, inode in enumerate(sockets):
            (fd / str(3 + n)).symlink_to(f"socket:[{inode}]")
        # Bait: everything another person's process would give away.
        (self.proc / str(pid) / "stat").write_text(f"{pid} (python) S 1 0 0\n")
        (self.proc / str(pid) / "cmdline").write_bytes(b"python\0serve_policy.py\0")

    def listen(self, port, inode):
        self.rows.append(
            f"0: 00000000:{port:04X} 00000000:0000 0A 00000000:00000000 "
            f"00:00000000 00000000 1000 0 {inode} 1 0 100 0 0"
        )
        self._write_table()

    def view(self):
        return own_units.OwnUnits(
            self.proc, cgroup_root=self.cgroups, systemctl=self.systemctl
        )

    def pids(self, ports, apps, budget=BUDGET):
        return gpumgr.policy_gpu_pids(ports, apps, budget_mib=budget, view=self.view())


def app(pid, mib=3000):
    return {"pid": pid, "name": "python", "memory_mib": mib}


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture
def ours_and_theirs(world):
    """Our policy unit: listener 100 and its GPU worker 101 on :8000. Somebody
    else: 200 listens on :8000 too (another address), 300 holds a copy of
    *our* listening socket (the trap a fd-based search falls into)."""
    world.unit(UNIT, [100, 101])
    world.process(100, sockets=[555])
    world.process(101)
    world.process(200, sockets=[556])
    world.process(300, sockets=[555])
    world.listen(8000, 555)
    world.listen(8000, 556)
    return world


class Recorder:
    """Every file-system path the code under test touches."""

    def __init__(self, monkeypatch):
        self.seen: list = []
        record = self.seen.append

        def wrap(fn):
            @functools.wraps(fn)
            def inner(path, *a, **k):
                record(os.fspath(path))
                return fn(path, *a, **k)

            return inner

        for name in ("scandir", "readlink", "listdir", "stat", "lstat"):
            monkeypatch.setattr(os, name, wrap(getattr(os, name)))
        monkeypatch.setattr(os.path, "realpath", wrap(os.path.realpath))
        monkeypatch.setattr(builtins, "open", wrap(builtins.open))
        for name in ("read_text", "read_bytes", "iterdir", "resolve", "readlink"):
            original = getattr(pathlib.Path, name)

            def method(self, *a, _orig=original, **k):
                record(os.fspath(self))
                return _orig(self, *a, **k)

            monkeypatch.setattr(pathlib.Path, name, method)

    def under(self, root) -> set:
        """The entries under ``root`` that were touched, as their first one
        or two parts ("100", "net/tcp")."""
        root = os.fspath(root).rstrip("/") + "/"
        out = set()
        for p in self.seen:
            if p.startswith(root):
                parts = p[len(root) :].split("/")
                out.add("/".join(parts[:2]) if parts[0] == "net" else parts[0])
        return out

    def real_proc(self) -> list:
        return [p for p in self.seen if p == "/proc" or p.startswith("/proc/")]


# --- (a) only our own pids and the socket tables are read -----------------------


def test_the_exemption_reads_only_our_own_pids_and_the_socket_tables(
    ours_and_theirs, monkeypatch
):
    w = ours_and_theirs
    rec = Recorder(monkeypatch)
    found = w.pids([8000], [app(100), app(101), app(200), app(300)])
    monkeypatch.undo()
    assert found == {100, 101}
    touched = rec.under(w.proc)
    assert touched <= {"100", "101", "net/tcp", "net/tcp6"}, touched
    assert not {"200", "300"} & touched
    assert rec.real_proc() == []


def test_the_policy_memory_reads_only_our_own_pids_and_the_socket_tables(
    ours_and_theirs, monkeypatch
):
    w = ours_and_theirs
    holders = [app(100, 10), app(101, 7000), app(200, 9000), app(300, 500)]
    rec = Recorder(monkeypatch)
    mib = gpumgr.policy_vram_mib([8000], holders=holders, view=w.view())
    monkeypatch.undo()
    assert mib == 7010
    touched = rec.under(w.proc)
    assert touched <= {"100", "101", "net/tcp", "net/tcp6"}, touched
    assert rec.real_proc() == []


def test_the_model_guardian_reads_only_our_own_pids_and_the_socket_tables(
    world, monkeypatch
):
    world.unit("levi-live", [400, 401])
    world.process(400, sockets=[777])  # vLLM's API server
    world.process(401)  # its engine core
    world.process(500, sockets=[777])  # somebody holding a copy of it
    world.listen(8100, 777)
    rec = Recorder(monkeypatch)
    served = gpu._served_pids({8100}, view=world.view(), records=[])
    monkeypatch.undo()
    assert served == {400, 401}
    touched = rec.under(world.proc)
    assert touched <= {"400", "401", "net/tcp", "net/tcp6"}, touched
    assert rec.real_proc() == []


def test_the_module_has_no_reader_of_another_pids_proc_left():
    for name in ("_cwd_inside", "_server_of", "_listener", "_serves", "_holds"):
        assert not hasattr(gpumgr, name), name
    for name in ("_serves", "_socket_inodes"):
        assert not hasattr(gpu, name), name


# --- (b) our unit's GPU processes are exempt, nobody else's ---------------------


def test_our_units_gpu_processes_are_exempt_and_nobody_elses(ours_and_theirs):
    w = ours_and_theirs
    assert w.pids([8000], [app(100), app(101), app(200), app(300)]) == {100, 101}
    # Without a budget too: a holder of the socket outside our cgroup is
    # never ours, whatever it holds.
    apps = [app(100), app(101), app(200), app(300)]
    assert w.pids([8000], apps, budget=0) == {100, 101}
    assert w.pids([8001], [app(100), app(101)]) == set()  # we do not serve there
    assert w.pids([], [app(100)]) == set()


def test_somebody_elses_server_on_our_port_is_never_exempt(world):
    # Same port, same size, holding the socket itself: not in our cgroup.
    world.unit(UNIT, [])
    world.process(200, sockets=[556])
    world.listen(8000, 556)
    assert world.pids([8000], [app(200, 7660)]) == set()


def test_a_unit_that_is_not_one_of_ours_owns_nothing(world):
    world.unit("somebody-policy", [100])
    world.process(100, sockets=[555])
    world.listen(8000, 555)
    assert world.pids([8000], [app(100)]) == set()


def test_a_unit_that_does_not_listen_on_the_port_is_not_exempt(world):
    world.unit(UNIT, [100])
    world.process(100)  # on the GPU, but holds no listening socket
    world.process(200, sockets=[555])
    world.listen(8000, 555)
    assert world.pids([8000], [app(100), app(200)]) == set()


def test_the_budget_is_per_unit_and_unknown_memory_is_not_exempt(world):
    world.unit(UNIT, [100, 101])
    world.unit("levi-aeri-policy-reset", [110])
    world.process(100, sockets=[555])
    world.process(101)
    world.process(110, sockets=[556])
    world.listen(8000, 555)
    world.listen(8001, 556)
    apps = [app(100, 5000), app(101, 5000), app(110, 7000)]
    # 10000 MiB for the first unit together: over 8500; the second fits.
    assert world.pids([8000, 8001], apps) == {110}
    assert world.pids([8000, 8001], apps, budget=0) == {100, 101, 110}
    apps[0] = {"pid": 100, "name": "python", "memory_mib": None}
    assert world.pids([8000, 8001], apps, budget=0) == {110}


def test_the_model_guardian_serves_only_our_model_units_processes(world):
    world.unit("levi-live", [400])
    world.process(400, sockets=[777])
    world.process(500, sockets=[778])
    world.listen(8100, 777)
    world.listen(8100, 778)
    assert gpu._served_pids({8100}, view=world.view(), records=[]) == {400}
    assert gpu._served_pids({8101}, view=world.view(), records=[]) == set()


# --- (c) a cgroup that cannot be read exempts nothing ---------------------------


def test_an_unreadable_cgroup_exempts_nothing(ours_and_theirs):
    w = ours_and_theirs
    w.unit("levi-aeri-policy-reset", None)  # cgroup.procs missing
    with pytest.raises(OSError):
        w.pids([8000], [app(100), app(101)])
    # Its memory is only bounded (second review: never "unknown" for sleeping).
    mib = gpumgr.policy_vram_mib([8000], holders=[app(100)], view=w.view())
    assert isinstance(mib, gpumgr.ForeignEstimate)


def test_systemd_that_cannot_be_asked_exempts_nothing(ours_and_theirs):
    w = ours_and_theirs
    w.listing_ok = False
    with pytest.raises(OSError):
        w.pids([8000], [app(100), app(101)])


def test_our_listener_whose_fds_cannot_be_read_exempts_nothing(world):
    world.unit(UNIT, [100, 101])  # 100 has no fd entry at all
    world.process(101)
    world.listen(8000, 555)
    assert world.pids([8000], [app(100), app(101)]) == set()


def test_the_model_guardian_counts_an_unreadable_unit_as_someone_else(world):
    world.unit("levi-live", None)
    world.listen(8100, 777)
    assert gpu._served_pids({8100}, view=world.view(), records=[]) == set()


# --- (d) the controller still refuses in every unknown case (review F1, C5) -------


def wired(ctl, world):
    ctl.probes.policy_pids = functools.partial(
        gpumgr.policy_gpu_pids, view=world.view()
    )


POLICY = POLICY_APP["pid"]


def test_somebody_elses_server_on_port_8000_still_yields_and_refuses(ctl, world):
    t = time.time()
    asleep_vllm(ctl, t)
    wired(ctl, world)
    world.unit(UNIT, [])
    world.process(POLICY, sockets=[555])
    world.listen(8000, 555)
    add_apps(ctl, [dict(POLICY_APP, memory_mib=8800)])
    ctl.tick(t + 1)
    assert {r.code for r in ctl.yield_state.reasons} == {"deployment", "foreign_gpu"}
    ok, code, why = ctl._online_wake(t + 2, "timeshare")
    assert (ok, code) == (False, "no_room") and "foreign_gpu_mib" in why


def test_our_own_unit_within_budget_is_exempt(ctl, world):
    t = time.time()
    asleep_vllm(ctl, t)
    wired(ctl, world)
    world.unit(UNIT, [POLICY])
    world.process(POLICY, sockets=[555])
    world.listen(8000, 555)
    add_apps(ctl, [POLICY_APP])
    ctl.tick(t + 1)
    assert {r.code for r in ctl.yield_state.reasons} == {"deployment"}
    fresh(ctl)
    assert ctl._online_wake(t + 2, "timeshare") is None


def test_an_unreadable_cgroup_keeps_the_yield_and_the_refusal(ctl, world):
    # Review F1: what cannot be resolved exempts nothing.
    t = time.time()
    asleep_vllm(ctl, t)
    wired(ctl, world)
    world.unit(UNIT, None)
    world.process(POLICY, sockets=[555])
    world.listen(8000, 555)
    add_apps(ctl, [POLICY_APP])
    ctl.tick(t + 1)
    assert "foreign_gpu" in {r.code for r in ctl.yield_state.reasons}
    ok, code, _ = ctl._online_wake(t + 2, "timeshare")
    assert (ok, code) == (False, "no_room")


def test_the_controller_asks_with_the_budget_and_no_folder_rule(ctl):
    asked = []
    t = time.time()
    asleep_vllm(ctl, t)

    def probe(ports, apps, **kw):
        asked.append(kw)
        return set()

    ctl.probes.policy_pids = probe
    add_apps(ctl, [POLICY_APP])
    ctl.tick(t + 1)
    assert asked and asked[-1] == {"budget_mib": ctl.config.gpu.policy_budget_mib}
    assert not hasattr(ctl.config.gpu, "policy_roots")


def test_our_own_unit_over_the_budget_is_not_exempt(ctl, world):
    t = time.time()
    asleep_vllm(ctl, t)
    wired(ctl, world)
    world.unit(UNIT, [POLICY])
    world.process(POLICY, sockets=[555])
    world.listen(8000, 555)
    ctl.config.gpu.policy_budget_mib = 7000
    add_apps(ctl, [POLICY_APP])
    ctl.tick(t + 1)
    assert "foreign_gpu" in {r.code for r in ctl.yield_state.reasons}


# --- second review --------------------------------------------------------------


def test_an_inactive_old_unit_does_not_hide_the_active_one(world):
    world.unit("levi-aeri-policy-old", [], active=False)  # ControlGroup ""
    world.unit(UNIT, [100])
    world.process(100, sockets=[555])
    world.listen(8000, 555)
    assert world.pids([8000], [app(100)]) == {100}


def test_a_unit_at_exactly_the_budget_is_still_exempt(world):
    world.unit(UNIT, [100, 101])
    world.process(100, sockets=[555])
    world.process(101)
    world.listen(8000, 555)
    apps = [app(100, 4000), app(101, BUDGET - 4000)]
    assert world.pids([8000], apps) == {100, 101}
    apps[1] = app(101, BUDGET - 3999)
    assert world.pids([8000], apps) == set()


def test_a_hand_started_server_in_our_scope_is_ours(world):
    world.unit("levi-aeri-policy-manual-pi05.scope", [100])
    world.process(100, sockets=[555])
    world.listen(8000, 555)
    assert world.pids([8000], [app(100)]) == {100}
    assert world.view().policy_units() == ["levi-aeri-policy-manual-pi05.scope"]


@pytest.mark.parametrize(
    "name",
    [
        "somebody-policy.scope",
        "levi-aeri-policy-.scope",
        "levi-aeri-policy-Manual.scope",
        "run-u42.scope",
        "run-levi-aeri-policy-x.scope",
        "levi-aeri-policy-x.scope.bak.scope",
        "levi-aeri-policy-x.slice",
    ],
)
def test_a_scope_whose_name_is_not_ours_owns_nothing(world, name):
    world.units[name] = ([100], f"/user.slice/{name}")
    folder = world.cgroups / "user.slice" / name
    folder.mkdir(parents=True)
    (folder / "cgroup.procs").write_text("100\n")
    world.process(100, sockets=[555])
    world.listen(8000, 555)
    assert world.pids([8000], [app(100)]) == set()


def test_policy_swap_validates_names_with_the_same_rule():
    from levi.automatic import fr3_policy_swap

    assert fr3_policy_swap.UNIT is own_units.POLICY_UNIT


def test_systemctl_output_that_is_not_text_is_a_failure(monkeypatch):
    def broken(*a, **k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")

    monkeypatch.setattr(own_units.subprocess, "run", broken)
    assert own_units._systemctl(["systemctl"])[0] == 125


# A listener that is not ours: its memory is bounded from nvidia-smi's rows.


def test_a_listener_not_ours_gives_a_conservative_bound(world, monkeypatch):
    world.unit(UNIT, [])
    world.process(200, sockets=[556])  # somebody's (or a hand-started) server
    world.listen(8000, 556)
    holders = [app(200, 7000), app(300, 1500), app(400, 2000)]  # 400: our vLLM
    rec = Recorder(monkeypatch)
    mib = gpumgr.policy_vram_mib(
        [8000], holders=holders, view=world.view(), exclude={400}
    )
    monkeypatch.undo()
    assert isinstance(mib, gpumgr.ForeignEstimate) and mib == 8500
    assert rec.under(world.proc) <= {"net/tcp", "net/tcp6"}
    # Our unit listening: its own memory, no bound.
    world.unit(UNIT, [200])
    mib = gpumgr.policy_vram_mib([8000], holders=holders, view=world.view())
    assert mib == 7000 and not isinstance(mib, gpumgr.ForeignEstimate)
    # Nobody listening: nothing to bound.
    assert gpumgr.policy_vram_mib([8001], holders=holders, view=world.view()) is None


def test_an_unreadable_cgroup_also_gives_the_bound(world):
    world.unit(UNIT, None)
    world.process(100, sockets=[555])
    world.listen(8000, 555)
    mib = gpumgr.policy_vram_mib([8000], holders=[app(100, 9000)], view=world.view())
    assert isinstance(mib, gpumgr.ForeignEstimate) and mib == 9000


def test_the_bound_puts_vllm_to_sleep_and_counts_for_the_budget():
    from levi.live import config as live_config

    c = live_config.Config()
    big = gpumgr.ForeignEstimate(9000)
    assert gpumgr.should_sleep(c, "timeshare", free_mib=None, policy_mib=big)[0] == (
        "policy_large"
    )
    assert gpumgr.policy_loaded(c, True, big) == "loaded"


def _wired_real(ctl, world, monkeypatch, *, leader=400, kids=(401,)):
    """The real policy lookup over the fake kernel tree; the vLLM record names
    ``leader`` (alive with its identity), whose children are ``kids``."""
    ctl.probes.policy_vram = gpumgr.policy_vram_mib
    ctl.own_view = world.view()
    record = {"pid": leader, "identity": {"start_ticks": 1, "boot": "b"}}
    monkeypatch.setattr(ctl.vllm, "_record", lambda: record)
    monkeypatch.setattr(ctl.vllm, "_pid", lambda: leader)  # its pid file agrees
    monkeypatch.setattr(
        gpumgr,
        "process_state",
        lambda pid, identity: "alive" if pid == leader else "gone",
    )
    world.process(leader, sockets=[])
    world.children(leader, list(kids))
    for k in kids:
        world.process(k)
        world.children(k, [])


def test_the_bound_leaves_our_own_vllm_out_without_listing_proc(
    ctl, world, monkeypatch
):
    world.unit(UNIT, [])
    world.process(200, sockets=[556])  # a hand-started server, in no unit
    world.listen(8000, 556)
    _wired_real(ctl, world, monkeypatch)
    holders = [app(200, 7000), app(400, 300), app(401, 2000), app(300, 1500)]
    rec = Recorder(monkeypatch)
    mib = ctl._policy_vram([8000], holders=holders)
    monkeypatch.undo()
    assert isinstance(mib, gpumgr.ForeignEstimate) and mib == 8500
    assert mib.note == ""
    assert rec.real_proc() == []
    assert rec.under(world.proc) <= {"400", "401", "net/tcp", "net/tcp6"}


def test_unknown_own_pids_count_our_vllm_too_and_the_reason_says_why(
    ctl, world, monkeypatch
):
    world.unit(UNIT, [])
    world.process(200, sockets=[556])
    world.listen(8000, 556)
    _wired_real(ctl, world, monkeypatch)

    def unreadable():
        raise ValueError("invalid vLLM owner record")

    monkeypatch.setattr(ctl.vllm, "_record", unreadable)
    holders = [app(200, 7000), app(401, 2000)]
    mib = ctl._policy_vram([8000], holders=holders)
    assert mib == 9000 and "could not be read" in mib.note
    why = gpumgr.should_sleep(ctl.config, "timeshare", free_mib=None, policy_mib=mib)
    assert why[0] == "policy_large" and "upper bound" in why[1]
    assert "could not be read" in why[1]


def test_a_tick_beside_our_own_policy_unit_reads_no_other_process(
    ctl, world, monkeypatch
):
    # SHOULD-A: with our unit serving :8000, no own-pid lookup at all (it
    # used to scan the whole of /proc for the vLLM's process group).
    t = time.time()
    asleep_vllm(ctl, t)
    ctl.config.gpu.yield_.enabled = False  # the yield rules' reading is apart
    world.unit(UNIT, [POLICY])
    world.process(POLICY, sockets=[555])
    world.process(999)  # somebody's process on the card
    world.listen(8000, 555)
    _wired_real(ctl, world, monkeypatch)
    monkeypatch.setattr(
        gpumgr,
        "gpu_holders",
        lambda runner=None: [dict(POLICY_APP), app(999, 4000)],
    )
    fresh(ctl)
    rec = Recorder(monkeypatch)
    ctl.tick(t + 1)
    ctl.policy_mib(t + 2, 0)
    ctl._online_wake(t + 3, "timeshare")
    monkeypatch.undo()
    assert ctl._policy_mib[1] == POLICY_APP["memory_mib"]
    assert not [p for p in rec.seen if p.rstrip("/") == "/proc"], "lists /proc"
    touched = rec.under(world.proc)
    assert touched <= {str(POLICY), "net/tcp", "net/tcp6"}, touched


def test_an_online_wake_says_the_listener_is_not_ours(ctl):
    t = time.time()
    asleep_vllm(ctl, t)
    ctl.probes.policy_vram = lambda ports: gpumgr.ForeignEstimate(7660)
    add_apps(ctl, [POLICY_APP])
    ctl.tick(t + 1)
    fresh(ctl)
    ok, code, why = ctl._online_wake(t + 2, "timeshare")
    assert (ok, code) == (False, "no_room")
    assert "not one of our levi-aeri-policy-* units" in why and ":8000" in why
    assert "setup.md" in why


# The live service started in a terminal: its vLLM is in no unit, but its
# owner record (vllm.json) is our registry.


@pytest.fixture
def recorded_vllm(world, tmp_path, monkeypatch):
    record = tmp_path / "vllm.json"
    record.write_text('{"pid": 400, "identity": {"start_ticks": 1, "boot": "b"}}')
    world.process(400, sockets=[777])  # vLLM's API server (the leader)
    world.process(401)  # its engine core on the GPU
    world.process(500, sockets=[777])  # somebody holding a copy of the socket
    world.children(400, [401])
    world.children(401, [])
    world.listen(8100, 777)
    states = {400: "alive"}
    monkeypatch.setattr(
        gpumgr, "process_state", lambda pid, identity: states.get(pid, "gone")
    )
    return record, states


def test_the_recorded_vllm_of_a_terminal_started_service_is_ours(
    world, recorded_vllm, monkeypatch
):
    record, _ = recorded_vllm
    rec = Recorder(monkeypatch)
    served = gpu._served_pids({8100}, view=world.view(), records=[record])
    monkeypatch.undo()
    assert served == {400, 401}
    assert rec.under(world.proc) <= {"400", "401", "net/tcp", "net/tcp6"}


def test_a_recorded_vllm_that_is_gone_or_unreadable_is_not_ours(
    world, recorded_vllm, tmp_path
):
    record, states = recorded_vllm
    states[400] = "gone"  # the pid was reused or it exited
    assert gpu._served_pids({8100}, view=world.view(), records=[record]) == set()
    states[400] = "alive"
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    missing = tmp_path / "missing.json"
    assert gpu._served_pids({8100}, view=world.view(), records=[broken]) == set()
    assert gpu._served_pids({8100}, view=world.view(), records=[missing]) == set()


def test_a_recorded_vllm_not_on_the_profiles_port_is_not_its_server(
    world, recorded_vllm
):
    record, _ = recorded_vllm
    assert gpu._served_pids({8101}, view=world.view(), records=[record]) == set()


def test_the_product_unit_is_never_a_model_unit(monkeypatch):
    monkeypatch.setenv("LEVI_LIVE_UNIT", "levi-live-levi-product")
    assert gpu.model_units() == []
    monkeypatch.setenv("LEVI_LIVE_UNIT", "levi-live-cold")
    assert gpu.model_units() == ["levi-live-cold.service"]
