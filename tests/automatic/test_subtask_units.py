"""Terminal D's own VLM unit and the numbered start/shutdown of a subtask_loop
run (levi/automatic/subtask_units.py) on fake systemd, /proc, nvidia-smi and
GPU lock. No unit tool, no GPU, no socket, nobody else's process."""

import json

import pytest
from fr3_swap_fakes import TOTAL_MIB, Proc, Smi, Time, Units, refuse_unit_tools

from levi.automatic import console_text as T
from levi.automatic import subtask_loop as st
from levi.automatic import subtask_units as U
from levi.automatic.campaign.switch import ProcView

UNIT = "levi-aeri-vlm-8100"
ARGV = [
    "/opt/vllm/bin/vllm",
    "serve",
    "/models/vlm",
    "--port",
    "8100",
    "--enable-sleep-mode",
]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    refuse_unit_tools(monkeypatch)


class StatProc(Proc):
    """The fake /proc with each process's ``stat`` (its starttime)."""

    def __init__(self, root):
        super().__init__(root)
        self.started = 1000

    def spawn(self, argv, cgroup, port=None):
        pid = super().spawn(argv, cgroup, port)
        self.started += 1
        fields = ["S"] + ["0"] * 18 + [str(self.started)]
        (self.root / str(pid) / "stat").write_text(f"{pid} (vllm) " + " ".join(fields))
        return pid


class Lock:
    def __init__(self, busy=False, trace=None):
        self.busy, self.held, self.trace = (
            busy,
            False,
            trace if trace is not None else [],
        )

    def acquire(self):
        self.trace.append("lock")
        if self.busy:
            return False
        self.held = True
        return True

    def release(self):
        self.trace.append("unlock")
        self.held = False


class Rig:
    def __init__(self, tmp_path, *, lock=None, power=None, mem=64.0, healthy=True):
        self.time = Time()
        self.proc = StatProc(tmp_path / "proc")
        self.smi = Smi()
        self.smi.pids = {}
        self.trace = []
        self.units = Units(
            self.proc, self.smi, hook=lambda argv: self.trace.append(argv[0])
        )
        self.time.tick = self.units.tick
        self.view = ProcView(
            self.proc.root, systemd=self.units, cgroup_root=self.proc.cgroup_root
        )
        self.healthy = healthy
        self.vlm = U.SystemdUnit(
            UNIT,
            ARGV,
            8100,
            systemd=self.units,
            proc=self.view,
            health=lambda port: self.healthy,
            clock=self.time.clock,
            sleep=self.time.sleep,
            poll_s=1.0,
        )
        self.lock = lock or Lock(trace=self.trace)
        self.lock.trace = self.trace
        self.power = power or st.FakeVlmPower()
        self.out = []
        self.t = T.Texts("en")
        self.services = U.Services(
            vlm=self.vlm,
            power=self.power,
            lock=self.lock,
            gpu=self.gpu,
            apps=lambda: [{"pid": p} for p in self.smi.pids],
            mem=lambda: mem,
            listeners=lambda port: self.view.listeners(port, (UNIT,)),
            say=self.out.append,
            t=self.t,
            record_dir=tmp_path / "units",
            policy_port=8000,
            need_policy_mib=7600,
            need_vlm_mib=22900,
            min_mem_gib=28,
            ready_timeout_s=10,
            clock=self.time.clock,
            sleep=self.time.sleep,
            poll_s=1.0,
            release_timeout_s=5,
        )

    def gpu(self):
        used = sum(self.smi.pids.values()) + self.smi.other_mib
        return {"total_mib": TOTAL_MIB, "used_mib": used, "free_mib": TOTAL_MIB - used}

    def step_keys(self):
        return [(r["step"], r["key"]) for r in self.services.records]


def test_the_start_order_and_a_clean_shutdown(tmp_path):
    r = Rig(tmp_path)
    s = r.services
    s.preflight()
    s.start_vlm()
    s.sleep_vlm()
    s.policy_ready()
    s.rounds()
    found = s.shutdown()
    assert found["released"] is True and found["stopped"] == [UNIT]
    assert [k for _, k in r.step_keys()] == [
        "preflight",
        "gpu",
        "mem",
        "ports",
        "vlm_start",
        "vlm_ready",
        "vlm_sleep",
        "vlm_asleep",
        "policy_ready",
        "inventory",
        "rounds",
        "shutdown",
        "released",
    ]
    assert [n for n, _ in r.step_keys()] == [1, 1, 1, 1, 2, 2, 3, 3, 4, 5, 6, 7, 7]
    # m25: the GPU lock first, released only after the release check.
    assert r.trace[0] == "lock" and r.trace[-1] == "unlock"
    assert r.power.sleeps == [1]
    assert all(
        line.startswith(f"{n}/7")
        for line, (n, _) in zip(r.out, r.step_keys(), strict=True)
    )
    assert json.loads((tmp_path / "units" / U.UNIT_FILE).read_text()) == {}
    assert r.proc.sockets == {} and r.units.units == {}


def test_the_identity_is_recorded_when_the_unit_is_up(tmp_path):
    """R15: unit, pid and starttime on disk before the next step."""
    r = Rig(tmp_path)
    r.services.preflight()
    r.services.start_vlm()
    kept = json.loads((tmp_path / "units" / U.UNIT_FILE).read_text())
    assert kept["unit"] == UNIT and kept["pid"] == r.units.units[UNIT]
    assert kept["starttime"] > 1000
    r.services.shutdown()


def test_a_port_held_by_someone_else_starts_nothing_and_stops_nothing(tmp_path):
    """R16 / m24."""
    r = Rig(tmp_path)
    other = r.proc.spawn(["someone-else"], "0::/user.slice/other.scope", 8100)
    with pytest.raises(U.ServiceFailed) as exc:
        r.services.preflight()
    assert exc.value.code == "port_held" and exc.value.stopped == []
    # Read-only systemctl show only: nothing started, nothing stopped.
    assert not [c for c in r.units.calls if c[0] == "systemd-run" or "stop" in c]
    assert other in r.proc.alive()
    assert any("Stop the live service" in line for line in r.out)
    assert r.lock.held is False


def test_the_policy_port_is_checked_too(tmp_path):
    r = Rig(tmp_path)
    r.proc.spawn(["policy"], "0::/user.slice/other.scope", 8000)
    with pytest.raises(U.ServiceFailed, match="port 8000"):
        r.services.preflight()


@pytest.mark.parametrize(
    "setup, code",
    [
        (lambda r: setattr(r.lock, "busy", True), "gpu_lock_busy"),
        (lambda r: setattr(r.smi, "other_mib", 10000), "gpu_memory_short"),
    ],
)
def test_preflight_refusals(tmp_path, setup, code):
    r = Rig(tmp_path)
    setup(r)
    with pytest.raises(U.ServiceFailed) as exc:
        r.services.preflight()
    assert exc.value.code == code
    assert "systemd-run" not in r.trace


def test_low_host_memory_refuses_at_preflight(tmp_path):
    r = Rig(tmp_path, mem=20.0)
    with pytest.raises(U.ServiceFailed) as exc:
        r.services.preflight()
    assert exc.value.code == "host_memory_short"


def test_a_vlm_that_never_listens_is_stopped_on_the_readiness_failure(tmp_path):
    """m21 / m22: readiness fails -> our unit stopped, nothing else."""
    r = Rig(tmp_path)
    r.units.silent = True
    r.services.preflight()
    with pytest.raises(U.ServiceFailed) as exc:
        r.services.start_vlm()
    assert exc.value.step == 2 and exc.value.code == "not_ready"
    assert exc.value.stopped == [UNIT] and r.units.units == {}
    assert r.lock.held is False


def test_an_unhealthy_vlm_never_counts_as_ready(tmp_path):
    r = Rig(tmp_path, healthy=False)
    r.services.preflight()
    with pytest.raises(U.ServiceFailed) as exc:
        r.services.start_vlm()
    assert exc.value.code == "not_ready" and exc.value.stopped == [UNIT]


def test_a_failed_sleep_stops_our_vlm(tmp_path):
    r = Rig(tmp_path, power=st.FakeVlmPower(sleep_ok=False))
    r.services.preflight()
    r.services.start_vlm()
    with pytest.raises(U.ServiceFailed) as exc:
        r.services.sleep_vlm()
    assert exc.value.step == 3 and exc.value.stopped == [UNIT]


def test_a_unit_whose_identity_changed_is_not_stopped(tmp_path):
    """R15 / m20: the unit's process is not the recorded one any more."""
    r = Rig(tmp_path)
    r.services.preflight()
    r.services.start_vlm()
    old = r.units.units[UNIT]
    group = r.proc.cgroups[old]
    r.proc.kill(old)
    new = r.proc.spawn(ARGV, "0::" + group, 8100)
    r.units.units[UNIT] = new
    found = r.services.shutdown()
    assert found["released"] is False and found["mismatch"] is True
    assert found["stopped"] == []
    assert ["systemctl", "--user", "stop", f"{UNIT}.service"] not in r.units.calls
    assert new in r.proc.alive()
    assert any("not stopped" in line for line in r.out)


def test_the_release_check_reports_memory_that_did_not_come_back(tmp_path):
    """R17 / m26: our pid still listed by nvidia-smi after the stop."""
    r = Rig(tmp_path)
    r.services.preflight()
    r.services.start_vlm()
    r.smi.hold_after_exit = 1000
    found = r.services.shutdown()
    assert found["released"] is False and found["left"]
    assert r.step_keys()[-1] == (7, "not_released")
    assert r.trace[-1] == "unlock"  # after the check


def test_a_leftover_unit_of_ours_is_stopped_only_when_its_identity_matches(tmp_path):
    r = Rig(tmp_path)
    r.services.preflight()
    r.services.start_vlm()
    # A crash: nothing was shut down; a new start finds the record.
    again = Rig.__new__(Rig)
    again.__dict__.update(r.__dict__)
    again.out = []
    again.services = U.Services(
        **{
            **{
                k: getattr(r.services, k)
                for k in (
                    "vlm",
                    "power",
                    "lock",
                    "gpu",
                    "apps",
                    "mem",
                    "listeners",
                    "say",
                    "t",
                    "policy_port",
                    "need_policy_mib",
                    "need_vlm_mib",
                    "min_mem_gib",
                    "ready_timeout_s",
                    "clock",
                    "sleep",
                    "poll_s",
                    "release_timeout_s",
                )
            },
            "record_dir": tmp_path / "units",
        }
    )
    r.lock.held = False
    again.services.preflight()
    assert ("leftover_stopped") in [k for _, k in again.step_keys()]
    assert r.units.units == {}


def test_the_texts_exist_in_both_languages():
    for key in ("svc_preflight", "svc_port_held", "svc_not_released", "svc_vlm_ready"):
        assert key in T.TEXT["zh"] and key in T.TEXT["en"]
