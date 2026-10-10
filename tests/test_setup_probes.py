"""Setup wizard status probes (``levi/setup/probes.py``) on a synthetic
``/proc`` tree and fake ``nvidia-smi``: what each probe reports, ``unknown``
for anything missing, the cache and the scan caps, and a socket guard that
proves no probe path connects anywhere or runs anything but a
``nvidia-smi --query-*``."""

import json
import os
import socket
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from levi.setup import probes as P

# ------------------------------------------------------------------ fixtures


def hexv4(ip, port):
    a = bytes(int(x) for x in ip.split("."))[::-1].hex().upper()
    return f"{a}:{port:04X}"


V6_LOOPBACK = "00000000000000000000000001000000"
V6_ANY = "0" * 32


def tcp_row(n, local, state="0A", inode="0"):
    return (
        f"   {n}: {local} 00000000:0000 {state} 00000000:00000000 00:00000000 "
        f"00000000  1000        0 {inode} 1 0000000000000000 100 0 0 10 0"
    )


HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"


class FakeProc:
    """A small /proc: tables, processes (stat, comm, cmdline, fd links)."""

    def __init__(self, root):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        (root / "net").mkdir(exist_ok=True)
        (root / "pressure").mkdir(exist_ok=True)
        (root / "self").mkdir(exist_ok=True)
        self.tcp, self.tcp6 = [], []
        self.flush()
        self.host()

    def flush(self):
        (self.root / "net" / "tcp").write_text("\n".join([HEADER, *self.tcp]) + "\n")
        (self.root / "net" / "tcp6").write_text("\n".join([HEADER, *self.tcp6]) + "\n")

    def listen(self, port, inode, ip="127.0.0.1", v6=None):
        if v6 is None:
            self.tcp.append(tcp_row(len(self.tcp), hexv4(ip, port), inode=inode))
        else:
            self.tcp6.append(tcp_row(len(self.tcp6), f"{v6}:{port:04X}", inode=inode))
        self.flush()

    def process(self, pid, comm, argv=(), ppid=1, start=1000, sockets=()):
        d = self.root / str(pid)
        (d / "fd").mkdir(parents=True, exist_ok=True)
        rest = ["S", str(ppid)] + ["0"] * 17 + [str(start)] + ["0"] * 20
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(rest) + "\n")
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        for n, inode in enumerate(sockets, start=3):
            os.symlink(f"socket:[{inode}]", d / "fd" / str(n))
        os.symlink("/dev/null", d / "fd" / "0")

    def host(self, cpu=(100, 0, 100, 800, 0, 0, 0, 0), psi_cpu=1.0, swap_used_kib=0):
        (self.root / "loadavg").write_text("1.50 1.20 1.00 2/700 12345\n")
        (self.root / "stat").write_text(
            "cpu  " + " ".join(map(str, cpu)) + " 0 0\ncpu0 1 1 1 1\n"
        )
        (self.root / "meminfo").write_text(
            "MemTotal:       131072000 kB\nMemFree:        1000 kB\n"
            "MemAvailable:   65536000 kB\nSwapTotal:      8388608 kB\n"
            f"SwapFree:       {8388608 - swap_used_kib} kB\n"
        )
        for kind, value in (("cpu", psi_cpu), ("memory", 0.0), ("io", 0.5)):
            (self.root / "pressure" / kind).write_text(
                f"some avg10={value:.2f} avg60=0.10 avg300=0.00 total=1\n"
                "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
            )
        (self.root / "self" / "mountinfo").write_text(
            "22 1 0:21 / /proc rw,nosuid shared:12 - proc proc rw\n"
        )


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def nvidia(gpu="0, NVIDIA RTX 5090, 32607, 20000, 37", apps=()):
    calls = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        assert cmd[0] == "nvidia-smi" and cmd[1].startswith("--query-"), cmd
        out = gpu if "--query-gpu" in cmd[1] else "\n".join(apps)
        return SimpleNamespace(stdout=out + "\n", returncode=0)

    run.calls = calls
    return run


def no_nvidia(cmd, **kwargs):
    raise FileNotFoundError("nvidia-smi")


@pytest.fixture
def proc(tmp_path):
    return FakeProc(tmp_path / "proc")


def probe(runner=no_nvidia, **kw):
    return P.Probes(
        runner=runner,
        clock=kw.pop("clock", Clock()),
        wall=kw.pop("wall", lambda: 2_000_000_000.0),
        **kw,
    )


# ------------------------------------------------------------------ ports


def test_ports_from_the_kernel_table_with_owners(proc):
    proc.listen(7861, "111")
    proc.listen(8100, "222", v6=V6_LOOPBACK)
    proc.listen(8000, "333", ip="0.0.0.0")
    proc.listen(9999, "444")
    proc.process(50, "python", sockets=["111"])
    proc.process(60, "vllm", sockets=["999", "222"])
    out = probe().status(P.Context(proc=str(proc.root)))["ports"]
    rows = {r["port"]: r for r in out["ports"]}
    assert rows[7861] == {
        "port": 7861,
        "role": "product_core",
        "listening": True,
        "scope": ["loopback"],
        "pid": 50,
        "process": "python",
        "owner_known": True,
    }
    assert rows[8100]["scope"] == ["loopback"] and rows[8100]["pid"] == 60
    assert rows[8000]["scope"] == ["any"] and rows[8000]["owner_known"] is False
    assert rows[5000]["listening"] is False
    assert 9999 not in rows  # only the ports the wizard shows


def test_ipv6_any_and_non_listen_rows(proc):
    proc.tcp6.append(tcp_row(0, f"{V6_ANY}:{5000:04X}", inode="5"))
    proc.tcp.append(tcp_row(0, hexv4("127.0.0.1", 8000), state="01", inode="6"))
    proc.flush()
    rows = {
        r["port"]: r
        for r in probe().status(P.Context(proc=str(proc.root)))["ports"]["ports"]
    }
    assert rows[5000]["listening"] and rows[5000]["scope"] == ["any"]
    assert rows[8000]["listening"] is False  # ESTABLISHED, not LISTEN


def test_owner_full_scan_is_rate_limited(proc, monkeypatch):
    proc.listen(8000, "777")  # another user's socket: never resolves
    for pid in range(100, 110):
        proc.process(pid, "x")
    scans = []
    real = P._socket_inodes
    monkeypatch.setattr(
        P, "_socket_inodes", lambda root, pid: scans.append(pid) or real(root, pid)
    )
    clock = Clock()
    p = probe(clock=clock)
    ctx = P.Context(proc=str(proc.root))
    p.status(ctx)
    first = len(scans)
    assert first >= 10
    for _ in range(5):
        clock.t += P.DEFAULT_TTL_S + 0.1
        p.status(ctx)
    assert len(scans) == first  # no new full scan within OWNER_RESCAN_S
    clock.t += P.OWNER_RESCAN_S
    p.status(ctx)
    assert len(scans) > first


def test_a_new_listener_is_resolved_at_once(proc):
    proc.listen(8000, "777")  # unresolvable
    clock = Clock()
    p = probe(clock=clock)
    ctx = P.Context(proc=str(proc.root))
    p.status(ctx)
    proc.listen(8100, "888")
    proc.process(60, "vllm", sockets=["888"])
    clock.t += P.DEFAULT_TTL_S + 0.1  # well within OWNER_RESCAN_S
    row = next(r for r in p.status(ctx)["ports"]["ports"] if r["port"] == 8100)
    assert row["pid"] == 60


def test_known_owner_is_rechecked_cheaply_and_dropped_when_gone(proc, tmp_path):
    proc.listen(7861, "111")
    proc.process(50, "python", sockets=["111"])
    clock = Clock()
    p = probe(clock=clock)
    ctx = P.Context(proc=str(proc.root))
    assert p.status(ctx)["ports"]["ports"][5]["pid"] == 50
    os.unlink(proc.root / "50" / "fd" / "3")
    clock.t += P.DEFAULT_TTL_S + 0.1
    row = next(r for r in p.status(ctx)["ports"]["ports"] if r["port"] == 7861)
    assert row["pid"] is None and row["owner_known"] is False


# ------------------------------------------------------------------ GPU


def test_gpu_processes_are_named_by_the_port_their_parent_serves(proc):
    proc.listen(8000, "333")
    proc.process(70, "python", sockets=["333"])
    proc.process(71, "python", ppid=70)  # the policy server's worker
    proc.process(80, "isaac", ppid=1)
    run = nvidia(
        apps=["71, /usr/bin/python3, 8000", "80, isaac-sim, 12000", "x, bad, 1"]
    )
    out = probe(runner=run).status(P.Context(proc=str(proc.root)))["gpu"]
    assert out["gpus"][0] == {
        "index": 0,
        "name": "NVIDIA RTX 5090",
        "total_mib": 32607,
        "used_mib": 20000,
        "free_mib": 12607,
        "util_pct": 37,
    }
    roles = {a["pid"]: (a["role"], a["name"]) for a in out["processes"]}
    assert roles == {71: ("policy_server", "python3"), 80: ("other", "isaac-sim")}
    assert all(c[0] == "nvidia-smi" for c in run.calls) and len(run.calls) == 2


def test_gpu_unknown_without_nvidia_smi(proc):
    assert probe().status(P.Context(proc=str(proc.root)))["gpu"]["state"] == "unknown"


def test_gpu_timeout_is_unknown(proc):
    def slow(cmd, **kwargs):
        assert kwargs["timeout"] <= P.NVIDIA_TIMEOUT_S
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    assert (
        probe(runner=slow).status(P.Context(proc=str(proc.root)))["gpu"]["state"]
        == "unknown"
    )


# ------------------------------------------------------------------ GPU locks


def test_lock_holders_from_proc_locks(proc, tmp_path):
    lock = tmp_path / "gpu.lock"
    lock.write_text("")
    st = os.stat(lock)
    key = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    proc.process(90, "flock")
    (proc.root / "locks").write_text(
        f"1: FLOCK  ADVISORY  WRITE 90 {key} 0 EOF\n"
        f"1: -> FLOCK  ADVISORY  WRITE 91 {key} 0 EOF\n"
        "2: POSIX  ADVISORY  READ 92 00:01:1 0 EOF\n"
    )
    ctx = P.Context(
        proc=str(proc.root),
        gpu_lock_files=(("levi", str(lock)), ("live", str(tmp_path / "absent.lock"))),
    )
    out = probe().status(ctx)["gpu_locks"]["locks"]
    assert out[0] == {
        "label": "levi",
        "state": "held",
        "holders": [{"pid": 90, "process": "flock", "kind": "FLOCK", "mode": "WRITE"}],
    }
    assert out[1]["state"] == "absent"
    (proc.root / "locks").write_text("")
    p = probe()
    assert p.status(ctx)["gpu_locks"]["locks"][0]["state"] == "free"
    assert lock.read_text() == ""  # never written, never locked
    assert (
        probe().status(P.Context(proc=str(proc.root)))["gpu_locks"]["state"]
        == "unknown"
    )


# ------------------------------------------------------------------ live service


def test_live_status_file(proc, tmp_path):
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps(
            {
                "pid": 4242,
                "state": "idle",
                "updated_at": 2_000_000_000.0 - 3,
                "workspace": "/secret/path",
                "gpu": {
                    "vllm_state": "asleep",
                    "mode": "timeshare",
                    "gate": {"open": False, "code": "robot_quiet", "reason": "r"},
                    "decision": {"allowed": False, "code": "manual", "reason": "x"},
                    "free_mib": 1000,
                },
            }
        )
    )
    proc.process(4242, "levi")
    out = probe().status(P.Context(proc=str(proc.root), live_status_file=str(status)))
    live = out["live"]
    assert live["alive"] is True and live["vllm_state"] == "asleep"
    assert live["gate"] == {"open": False, "code": "robot_quiet", "reason": "r"}
    assert "/secret/path" not in json.dumps(out)
    assert out["guard"]["live_gate"] == {"code": "robot_quiet", "robot_quiet": True}
    status.write_text("{broken")
    p = probe()
    assert (
        p.status(P.Context(proc=str(proc.root), live_status_file=str(status)))["live"][
            "state"
        ]
        == "unknown"
    )
    assert probe().status(P.Context(proc=str(proc.root)))["live"]["state"] == "unknown"


# ------------------------------------------------------------------ host


def test_host_cpu_memory_pressure(proc):
    clock = Clock()
    p = probe(clock=clock)
    ctx = P.Context(proc=str(proc.root))
    first = p.status(ctx)["host"]
    assert first["cpu_busy_pct"] is None  # needs two samples
    assert first["load"] == [1.5, 1.2, 1.0]
    assert first["memory"]["available_gib"] == 62.5
    assert first["pressure"]["cpu"]["some_avg10"] == 1.0
    proc.host(cpu=(150, 0, 150, 900, 0, 0, 0, 0))  # +100 busy, +100 idle
    clock.t += P.DEFAULT_TTL_S + 0.1
    assert p.status(ctx)["host"]["cpu_busy_pct"] == 50.0


# ------------------------------------------------------------------ ROS logs


def test_ros_alarm_counts_in_the_window(proc, tmp_path):
    logs = tmp_path / "ros"
    logs.mkdir()
    now = 2_000_000_000.0
    (logs / "ros2_control_node_1_1999999000000.log").write_text(
        f"[ERROR] [{now - 10:.3f}] [fr3]: Control stopped: communication_constraints_violation\n"
        f"[WARN] [{now - 9:.3f}] [cm]: Overrun detected! The control loop took 1.2 ms\n"
        f"[WARN] [{now - 8:.3f}] [cm]: Overrun detected! The control loop took 1.3 ms\n"
        f"[ERROR] [{now - 7:.3f}] [fr3]: Motion aborted by reflex! [cartesian_reflex]\n"
        f"[INFO] [{now - 6:.3f}] [fr3]: ordinary line\n"
        f"[ERROR] [{now - 7200:.3f}] [fr3]: old communication_constraints_violation\n"
        "not a ros line\n"
    )
    (logs / "other.log").write_text(
        "[ERROR] [1.0] [x]: communication_constraints_violation\n"
    )
    for path in logs.iterdir():
        os.utime(path, (now, now))
    p = probe(wall=lambda: now)
    out = p.status(P.Context(proc=str(proc.root), ros_log_dir=str(logs)))["ros"]
    assert out["counts"] == {"comm_violation": 1, "overrun": 2, "cartesian_reflex": 1}
    assert out["files"] == 1
    missing = probe().status(
        P.Context(proc=str(proc.root), ros_log_dir=str(tmp_path / "no"))
    )
    assert missing["ros"]["state"] == "unknown"
    assert probe().status(P.Context(proc=str(proc.root)))["ros"]["state"] == "unknown"


def test_ros_reads_only_a_bounded_tail(proc, tmp_path):
    logs = tmp_path / "ros"
    logs.mkdir()
    now = 2_000_000_000.0
    line = f"[WARN] [{now - 1:.3f}] [cm]: Overrun detected! padding padding\n"
    (logs / "ros2_control_node_1_1999999000000.log").write_text(line * 20000)
    os.utime(logs / "ros2_control_node_1_1999999000000.log", (now, now))
    out = probe(wall=lambda: now).status(
        P.Context(proc=str(proc.root), ros_log_dir=str(logs))
    )
    assert out["ros"]["counts"]["overrun"] <= P.ROS_TAIL_BYTES // len(line) + 1


# ------------------------------------------------------------------ health, disks, recorder


def test_fr3_health(proc, tmp_path):
    health = tmp_path / "fr3_health.json"
    health.write_text(
        json.dumps(
            {
                "updated_at_epoch": 2_000_000_000.0 - 1,
                "controller_active": True,
                "hardware_active": True,
            }
        )
    )
    out = probe().status(P.Context(proc=str(proc.root), health_file=str(health)))
    assert (
        out["fr3_health"]["state"] == "ok"
        and out["fr3_health"]["controller_active"] is True
    )
    gone = probe().status(
        P.Context(proc=str(proc.root), health_file=str(tmp_path / "none.json"))
    )
    assert gone["fr3_health"]["state"] == "missing"
    assert (
        probe().status(P.Context(proc=str(proc.root)))["fr3_health"]["state"]
        == "unknown"
    )


def test_disks_have_labels_not_paths(proc, tmp_path):
    out = probe().status(
        P.Context(
            proc=str(proc.root),
            disks=(("tmp", str(tmp_path)), ("gone", str(tmp_path / "x"))),
        )
    )
    rows = out["disks"]["disks"]
    assert rows[0]["label"] == "tmp" and rows[0]["free_gib"] >= 0
    assert rows[1] == {"label": "gone", "state": "unknown"}
    assert str(tmp_path) not in json.dumps(out)


def test_recorder_state(proc, tmp_path):
    base = tmp_path / "fr3rec"
    data = base / "data"
    data.mkdir(parents=True)
    proc.process(321, "python3", start=5555)
    (data / "recorder.pid").write_text(
        json.dumps({"pid": 321, "start": 5555, "started": 1})
    )
    (data / "status.json").write_text(
        json.dumps(
            {
                "state": "running",
                "updated": 2_000_000_000.0 - 2,
                "snapshots": 3,
                "disk_ok": True,
            }
        )
    )
    ctx = P.Context(proc=str(proc.root), recorder_dir=str(base))
    out = probe().status(ctx)["recorder"]
    assert out == {
        "state": "running",
        "pid": 321,
        "age_s": 2.0,
        "snapshots": 3,
        "disk_ok": True,
    }
    (data / "recorder.pid").write_text(
        json.dumps({"pid": 321, "start": 1})
    )  # PID reused
    assert probe().status(ctx)["recorder"]["state"] == "stopped"
    assert (
        probe().status(P.Context(proc=str(proc.root)))["recorder"]["state"] == "unknown"
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    assert (
        probe().status(P.Context(proc=str(proc.root), recorder_dir=str(empty)))[
            "recorder"
        ]["state"]
        == "unknown"
    )


# ------------------------------------------------------------------ guard


def test_robot_quiet_guard(proc, tmp_path):
    proc.process(
        500,
        "python3",
        ["python3", "-m", "fr3_robot_server.franka_server_ros2", "--token=SECRET"],
    )
    proc.process(501, "grep", ["grep", "franka_server"])
    proc.process(
        502,
        "nice",
        ["nice", "-n", "15", "python3", "scripts/serve_policy.py", "--key", "SECRET2"],
    )
    out = probe().status(P.Context(proc=str(proc.root)))
    guard = out["guard"]
    assert guard["quiet"] is False
    assert sorted(r["name"] for r in guard["robot"]) == [
        "franka_server_ros2",
        "serve_policy",
    ]
    text = json.dumps(out)
    assert "SECRET" not in text and "--token" not in text  # no command line leaves


def test_guard_banner_while_the_controller_is_active(proc, tmp_path):
    health = tmp_path / "h.json"
    health.write_text(
        json.dumps({"updated_at_epoch": 2_000_000_000.0, "controller_active": True})
    )
    proc.host(psi_cpu=40.0, swap_used_kib=1024 * 300)
    p = probe(jobs=lambda: [{"kind": "pool", "id": "export-1", "pid": 9}])
    guard = p.status(P.Context(proc=str(proc.root), health_file=str(health)))["guard"]
    assert guard["quiet"] is False and guard["controller_active"] is True
    reasons = " | ".join(guard["banner"]["reasons"])
    assert (
        "product jobs" in reasons
        and "swap in use" in reasons
        and "cpu pressure" in reasons
    )
    assert guard["product_jobs"] == [{"kind": "pool", "id": "export-1"}]


def test_guard_quiet_and_unknown(proc):
    proc.process(7, "bash", ["bash"])
    assert probe().status(P.Context(proc=str(proc.root)))["guard"]["quiet"] is True
    (proc.root / "self" / "mountinfo").write_text(
        "22 1 0:21 / /proc rw - proc proc rw,hidepid=2\n"
    )
    guard = probe().status(P.Context(proc=str(proc.root)))["guard"]
    assert guard["quiet"] is None and guard["banner"] is None


@pytest.mark.parametrize(
    "argv, names",
    [
        (["ros2", "launch", "pkg", "x.launch.py"], ["ros2", "x.launch"]),
        (["uv", "run", "scripts/serve_policy.py"], ["uv", "serve_policy"]),
        (["python3", "-c", "import franka_server"], ["python3"]),
        (["/usr/bin/franka_server"], ["franka_server"]),
        (["timeout", "5", "bash", "run.sh"], ["timeout", "bash", "run"]),
    ],
)
def test_command_names(argv, names):
    assert P.command_names(argv) == names


# ------------------------------------------------------------------ cache, bounds, missing


def test_lazy_cache_and_ttl(proc):
    clock = Clock()
    p = probe(clock=clock)
    ctx = P.Context(proc=str(proc.root))
    assert p.samples == 0  # nothing samples until asked
    a = p.status(ctx)
    b = p.status(ctx)
    assert p.samples == 1 and a["cached"] is False and b["cached"] is True
    assert b["sampled_at"] == a["sampled_at"]
    clock.t += P.DEFAULT_TTL_S + 0.01
    p.status(ctx)
    assert p.samples == 2
    p.status(P.Context(proc=str(proc.root), ros_log_dir="/elsewhere"))
    assert p.samples == 3  # another context is another sample


def test_ttl_has_a_floor(proc):
    clock = Clock()
    p = probe(clock=clock, ttl_s=0)
    ctx = P.Context(proc=str(proc.root))
    p.status(ctx)
    clock.t += P.MIN_TTL_S / 2
    p.status(ctx)
    assert p.samples == 1


def test_concurrent_requests_share_one_sample(proc):
    gate = threading.Event()

    def slow(cmd, **kwargs):
        gate.wait(5)
        raise FileNotFoundError

    p = P.Probes(runner=slow)
    ctx = P.Context(proc=str(proc.root))
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(p.status(ctx))) for _ in range(8)
    ]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(10)
    assert len(results) == 8 and p.samples == 1
    assert len({r["sampled_at"] for r in results}) == 1
    assert sum(1 for r in results if not r["cached"]) == 1


def test_everything_missing_is_unknown_not_an_error(tmp_path):
    out = probe().status(
        P.Context(
            proc=str(tmp_path / "noproc"),
            live_status_file=str(tmp_path / "s.json"),
            gpu_lock_files=(("levi", str(tmp_path / "l")),),
            health_file=str(tmp_path / "h.json"),
            ros_log_dir=str(tmp_path / "ros"),
            recorder_dir=str(tmp_path / "rec"),
            disks=(("x", str(tmp_path / "nope")),),
        )
    )
    for key in ("ports", "gpu", "live", "host", "ros", "recorder"):
        assert out[key]["state"] == "unknown", key
    assert out["fr3_health"]["state"] == "missing"
    assert out["gpu_locks"]["locks"][0]["state"] == "absent"
    assert out["guard"]["quiet"] is None


def test_a_crashing_probe_does_not_fail_the_rest(proc, monkeypatch):
    monkeypatch.setattr(P, "listening", lambda root: 1 / 0)
    out = probe().status(P.Context(proc=str(proc.root)))
    assert out["ports"] == {
        "state": "unknown",
        "detail": "probe failed: ZeroDivisionError",
    }
    assert out["host"]["state"] == "ok"


# ------------------------------------------------------------------ the socket guard


ROBOT_PORTS = (5000, 5001, 5100, 7470, 8000)


@pytest.fixture
def no_network(monkeypatch):
    """Every way out is closed: a connect or a subprocess other than
    nvidia-smi --query-* fails the test."""
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise AssertionError(f"a probe tried to connect: {args[1:]}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    spawned = []

    def guarded_run(cmd, *args, **kwargs):
        spawned.append(list(cmd))
        assert cmd[0] == "nvidia-smi" and all(
            a.startswith(("--query-", "--format=")) for a in cmd[1:]
        ), cmd
        return SimpleNamespace(stdout="", returncode=0)

    def no_popen(*args, **kwargs):
        raise AssertionError(f"a probe started a process: {args}")

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(subprocess, "Popen", no_popen)
    monkeypatch.setattr(os, "system", no_popen)
    return SimpleNamespace(attempts=attempts, spawned=spawned)


def full_context(proc, tmp_path):
    for port, inode in zip(ROBOT_PORTS + (7861, 8100), range(100, 120)):
        proc.listen(port, str(inode))
    proc.process(42, "python", sockets=["100", "104"])
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"pid": 42, "updated_at": time.time(), "gpu": {}}))
    health = tmp_path / "h.json"
    health.write_text(
        json.dumps({"updated_at_epoch": time.time(), "controller_active": True})
    )
    lock = tmp_path / "gpu.lock"
    lock.write_text("")
    (proc.root / "locks").write_text("")
    return P.Context(
        proc=str(proc.root),
        live_status_file=str(status),
        gpu_lock_files=(("levi", str(lock)),),
        health_file=str(health),
        ros_log_dir=str(tmp_path),
        recorder_dir=str(tmp_path),
        disks=(("tmp", str(tmp_path)),),
    )


def test_no_probe_connects_or_spawns(proc, tmp_path, no_network):
    """The whole sampling path, every probe, with the robot ports listening:
    nothing connects; the only process is nvidia-smi --query-*."""
    ctx = full_context(proc, tmp_path)
    p = P.Probes()  # the real subprocess.run, which the guard replaced
    out = p.status(ctx)
    assert {r["port"] for r in out["ports"]["ports"] if r["listening"]} >= set(
        ROBOT_PORTS
    )
    assert no_network.attempts == []
    assert no_network.spawned and all(c[0] == "nvidia-smi" for c in no_network.spawned)


def test_the_route_reads_only(proc, tmp_path, no_network, monkeypatch):
    """GET /api/levi/setup/status through the live router: the same guard,
    one sample for two requests, and nothing written."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from levi.live import api

    ctx = full_context(proc, tmp_path)
    monkeypatch.setattr(P, "context_from_environment", lambda: ctx)
    monkeypatch.setattr(P, "PROBES", P.Probes(jobs=list))
    before = sorted(
        (p, p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()
    )
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    first = client.get("/api/levi/setup/status")
    second = client.get("/api/levi/setup/status")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["cached"] is False and second.json()["cached"] is True
    assert P.PROBES.samples == 1
    assert no_network.attempts == []
    after = sorted(
        (p, p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()
    )
    assert after == before
    assert client.post("/api/levi/setup/status").status_code == 405


def test_context_from_a_live_workspace(tmp_path, monkeypatch):
    from levi.live import locate

    ws = tmp_path / "live-ws"
    (ws / "live").mkdir(parents=True)
    (ws / "live.toml").write_text(
        "[fr3]\nhealth_file = '/run/fr3_health.json'\n"
        "[gpu]\nlock_file = '/tmp/x.lock'\npolicy_ports = [8000, 8001]\n"
        "[watch]\nroots = ['/data/rollouts']\n"
    )
    monkeypatch.setattr(locate, "find", lambda own: locate.Found(ws, "env"))
    monkeypatch.setattr(locate, "is_live", lambda w: True)
    monkeypatch.setenv(locate.ENV_HOME, str(tmp_path / "home"))
    monkeypatch.setenv(P.ENV_ROS_LOG_DIR, str(tmp_path / "ros"))
    monkeypatch.setenv(P.ENV_RECORDER_DIR, str(tmp_path / "rec"))
    monkeypatch.delenv(P.ENV_GPU_LOCK, raising=False)
    c = P._build_context()
    assert c.live_status_file == str(tmp_path / "home" / "status.json")
    assert c.health_file == "/run/fr3_health.json"
    assert c.gpu_lock_files == (("live", "/tmp/x.lock"),)
    assert dict(c.ports)[8001] == "policy_server"
    assert [label for label, _ in c.disks] == [
        "product_workspace",
        "live_workspace",
        "rollout_root_1",
        "tmp",
    ]
    assert c.ros_log_dir == str(tmp_path / "ros") and c.recorder_dir == str(
        tmp_path / "rec"
    )
    assert not (ws / "live" / "effective.toml").exists()


def test_context_without_a_live_workspace(monkeypatch, tmp_path):
    from levi.live import locate

    monkeypatch.setattr(
        locate, "find", lambda own: locate.Found(None, "status", "not_configured")
    )
    monkeypatch.setenv(P.ENV_GPU_LOCK, str(tmp_path / "g.lock"))
    monkeypatch.delenv(P.ENV_RECORDER_DIR, raising=False)
    c = P._build_context()
    assert c.live_status_file is None and c.health_file is None
    assert c.gpu_lock_files == (("levi", str(tmp_path / "g.lock")),)
    assert c.recorder_dir is None


def test_the_service_serves_the_route_behind_the_ui_token(
    proc, tmp_path, no_network, monkeypatch
):
    """Mounted in the real service: the ordinary local authentication (the
    web UI token) applies, as to every other route."""
    from fastapi.testclient import TestClient

    from levi.service import app

    ctx = full_context(proc, tmp_path)
    monkeypatch.setattr(P, "context_from_environment", lambda: ctx)
    monkeypatch.setattr(P, "PROBES", P.Probes(jobs=list))
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    client = TestClient(app)
    assert client.get("/api/levi/setup/status").status_code == 401
    ok = client.get(
        "/api/levi/setup/status", headers={"x-levi-ui-token": "test-ui-token"}
    )
    assert ok.status_code == 200 and ok.json()["ports"]["state"] == "ok"
    assert no_network.attempts == []
