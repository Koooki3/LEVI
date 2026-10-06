"""Configuration, the robot side's files, the status contract, the read-only
API, and the service as a real process: start, idle, status, stop."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import scaled, with_netguard
from live_helpers import Rollouts

from levi.live import api, cli, controller, locate, mirror, resources, sessions
from levi.live import config as live_config

PROJECT = Path(__file__).resolve().parents[1]


def cfg(tmp_path, **watch):
    c = live_config.Config()
    c.service.workspace = str(tmp_path / "ws")
    c.service.home = str(tmp_path / "home")
    c.watch.roots = [str(tmp_path / "rollouts")]
    c.watch.backlog = "process"
    c.watch.settle_s = 0.0
    c.gpu.mode = "manual"
    c.gpu.lock_file = ""
    for key, value in watch.items():
        setattr(c.watch, key, value)
    return c


# --- configuration ----------------------------------------------------------------


def test_the_rendered_file_reads_back_as_the_same_configuration(tmp_path):
    text = live_config.render()
    path = tmp_path / "live.toml"
    path.write_text(text)
    assert live_config.load(path) == live_config.Config(path=str(path))
    assert "auto_approve = false" in text and "busy_states" in text


@pytest.mark.parametrize(
    "toml,message",
    [
        ("[gpu]\nmodle = 1\n", "Unknown key"),
        ("[gpu]\nmode = 'sometimes'\n", "gpu.mode"),
        ("[service]\nui_port = 7861\n", "reserved"),
        ("[service]\nui_port = 7880\ncore_port = 7880\n", "must differ"),
        ("[gpu]\npolicy_ports = [5000]\n", "5000"),
        ("[watch]\nbacklog = 'maybe'\n", "backlog"),
        ("[pipeline]\nauto_approve = 'yes'\n", "must be bool"),
        ("[pipeline]\ntemporal = 'no'\n", "must be bool"),
        ("[nothing]\na = 1\n", "Unknown table"),
    ],
)
def test_a_wrong_setting_is_an_error_not_a_default(tmp_path, toml, message):
    path = tmp_path / "live.toml"
    path.write_text(toml)
    with pytest.raises(ValueError, match=message):
        live_config.load(path)


def test_the_approver_is_off_by_default_and_ports_avoid_the_robot_and_the_product():
    c = live_config.Config()
    assert c.pipeline.auto_approve is False
    assert (c.service.ui_port, c.service.core_port) == (7880, 7881)
    assert c.gpu.mode == "auto" and c.effective_gpu_mode() == "timeshare"


# --- review only (pipeline.temporal) -----------------------------------------------


def test_time_segments_are_on_by_default_and_the_file_says_so(tmp_path):
    c = live_config.Config()
    assert c.pipeline.temporal is True
    assert "temporal = true" in live_config.render(c)
    c.pipeline.temporal = False
    c.pipeline.anchored_spec = "generic-release.v3.json"
    path = tmp_path / "live.toml"
    path.write_text(live_config.render(c))
    back = live_config.load(path)
    assert back.pipeline.temporal is False
    assert back.pipeline.anchored_spec == "generic-release.v3.json"
    assert back == live_config.Config(
        path=str(path),
        pipeline=live_config.Pipeline(
            temporal=False, anchored_spec="generic-release.v3.json"
        ),
    )


@pytest.mark.parametrize(
    "toml,message",
    [
        ("temporal = false\nanchored = false\n", "needs pipeline.anchored = true"),
        (
            "temporal = false\nanchored_spec = 'generic-release.v2.json'\n",
            "cannot use generic-release.v2.json: its episode.require_place",
        ),
        (
            "temporal = false\nanchored_spec = 'generic-release.v9.json'\n",
            "'generic-release.v9.json' cannot be read",
        ),
        (
            "temporal = false\nanchored_spec = '../config.py'\n",
            "cannot be read",
        ),
    ],
)
def test_review_only_needs_a_review_that_reads_no_time_segments(
    tmp_path, toml, message
):
    path = tmp_path / "live.toml"
    path.write_text("[pipeline]\n" + toml)
    with pytest.raises(ValueError, match=message):
        live_config.load(path)


@pytest.mark.parametrize(
    "toml",
    [
        "temporal = false\nanchored_spec = 'generic-release.v3.json'\n",
        "temporal = false\n",  # version 1, the default: no place condition
        "anchored_spec = 'generic-release.v2.json'\n",  # on: unchanged
        "anchored = false\n",  # on: unchanged
    ],
)
def test_review_only_settings_that_can_run(tmp_path, toml):
    path = tmp_path / "live.toml"
    path.write_text("[pipeline]\n" + toml)
    live_config.load(path)


def test_the_status_says_what_each_batch_runs(tmp_path):
    c = cfg(tmp_path)
    status = controller.Controller(c, log=lambda *a: None).status()
    assert status["pipeline"] == {
        "temporal": True,
        "anchored": True,
        "anchored_spec": "generic-release.v1.json",
    }
    c.pipeline.temporal = False
    c.pipeline.anchored_spec = "generic-release.v3.json"
    status = controller.Controller(c, log=lambda *a: None).status()
    assert status["pipeline"]["temporal"] is False
    assert status["pipeline"]["anchored_spec"] == "generic-release.v3.json"


# --- the robot side's files ---------------------------------------------------------


@pytest.fixture
def rollouts(tmp_path, demo_template):
    return Rollouts(tmp_path / "rollouts", demo_template)


def test_a_session_is_active_crashed_or_done(rollouts):
    now = time.time()
    rollouts.session("running")
    (s,) = sessions.read_sessions([rollouts.root], now).values()
    assert s.active() and not s.crashed and s.levi_enabled is True
    # Silent for 30 s and its process gone: crashed.
    rollouts.session("running", updated_at=now - 30, pid=2**22 + 12345)
    (s,) = sessions.read_sessions([rollouts.root], now).values()
    assert s.crashed and s.state == "crashed" and not s.active()
    # An hour old is crashed even if the pid was recycled.
    rollouts.session("running", updated_at=now - 7200)
    (s,) = sessions.read_sessions([rollouts.root], now).values()
    assert s.crashed
    rollouts.session("finished", updated_at=now - 7200)
    (s,) = sessions.read_sessions([rollouts.root], now).values()
    assert not s.crashed and not s.active()
    rollouts.session("fault", reason="robot reflex")
    (s,) = sessions.read_sessions([rollouts.root], now).values()
    assert s.fault and s.public()["reason"] == "robot reflex"


def test_a_session_tells_the_page_which_model_runs_without_a_path(rollouts):
    rollouts.session("waiting_reset")
    (s,) = sessions.read_sessions([rollouts.root], time.time()).values()
    public = s.public()
    assert public["policy"] == {
        "config": "pi05_fr3_all_state",
        "checkpoint": "pi05_fr3_all_step49999",
    }
    assert public["reset_wait_s"] == 10.0 and public["started_at"] is not None
    assert "/ckpt" not in json.dumps(public)


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("dual_label", "dual_label"),
        ("unattended", "unattended"),
        (None, None),
        ("dual", None),
        (["dual_label"], None),
        (7, None),
    ],
)
def test_a_session_says_how_its_episodes_are_labelled(rollouts, mode, expected):
    rollouts.session("waiting_reset")
    path = rollouts.root / ".eval_sessions" / f"{rollouts.group}__{rollouts.task}.json"
    data = json.loads(path.read_text())
    if mode is not None:
        data["levi"]["mode"] = mode
    path.write_text(json.dumps(data))
    (s,) = sessions.read_sessions([rollouts.root], time.time() + 1).values()
    assert s.label_mode == expected
    assert s.public()["label_mode"] == expected


def test_iso_and_epoch_times_are_both_read():
    assert sessions.parse_time(5.5) == 5.5
    assert sessions.parse_time("2026-10-01T10:00:00") is not None
    assert sessions.parse_time("2026-10-01T10:00:00Z") is not None
    assert sessions.parse_time("") is None and sessions.parse_time("junk") is None


def test_fr3_health_missing_or_stale_is_offline_not_a_red_light(tmp_path):
    path = tmp_path / "fr3_health.json"
    now = time.time()
    assert sessions.read_fr3(path, 3.0, now)["state"] == "missing"
    path.write_text(
        json.dumps(
            {"updated_at_epoch": now, "red_light": False, "robot_mode_name": "Idle"}
        )
    )
    assert sessions.read_fr3(path, 3.0, now)["state"] == "ok"
    path.write_text(
        json.dumps(
            {"updated_at_epoch": now, "red_light": True, "reasons": ["robot_mode 4"]}
        )
    )
    red = sessions.read_fr3(path, 3.0, now)
    assert red["state"] == "red" and red["reasons"] == ["robot_mode 4"]
    path.write_text(json.dumps({"updated_at_epoch": now - 10, "red_light": True}))
    assert sessions.read_fr3(path, 3.0, now)["state"] == "offline"


# --- the status contract (what the evaluation client checks) -----------------------------


USABLE = ("idle", "active", "annotating", "gpu_wait")


def client_accepts(status, rollout_root, now):
    """The evaluation client's own check (levi_live_link.check_service)."""
    root = Path(rollout_root).resolve()
    covers = any(
        root == Path(w).resolve()
        or Path(w).resolve() in root.parents
        or root in Path(w).resolve().parents
        for w in status.get("watch_roots") or []
    )
    return (
        str(status.get("schema", "")).startswith("levi.live.status")
        and now - status["updated_at"] <= 15
        and os.path.exists(f"/proc/{status['pid']}")
        and status.get("accepts_sessions")
        and status.get("state") in USABLE
        and covers
        and all(os.path.isabs(w) for w in status["watch_roots"])
    )


def test_the_status_file_satisfies_the_client_s_check(tmp_path, rollouts):
    c = cfg(tmp_path)
    ctl = controller.Controller(c, log=lambda *a: None)
    status = ctl.status()
    assert status["state"] == "starting" and not client_accepts(
        status, rollouts.root, time.time()
    )
    ctl.state = "idle"
    assert client_accepts(ctl.status(), rollouts.root, time.time())
    assert client_accepts(
        ctl.status(), rollouts.root / "sub/dir", time.time()
    )  # a descendant root
    ctl.state = "gpu_wait"
    assert client_accepts(ctl.status(), rollouts.root, time.time())
    for state in ("error", "stopped"):
        ctl.state = state
        assert not client_accepts(ctl.status(), rollouts.root, time.time())
    ctl.state = "idle"
    assert not client_accepts(ctl.status(), tmp_path / "elsewhere", time.time())
    ctl.write_status()
    assert json.loads(c.status_file.read_text())["pid"] == os.getpid()


def test_status_and_its_parts_stay_small(tmp_path, rollouts):
    c = cfg(tmp_path)
    c.resources.status_max_datasets = 5
    for n in range(12):
        other = Rollouts(
            rollouts.root, rollouts.source, task=f"task_{n:02d}", text=f"task {n}"
        )
        other.write(0)
        other.session("running")
    ctl = controller.Controller(c, log=lambda *a: None)
    ctl._refresh(time.time(), True)
    status = ctl.status()
    assert len(status["datasets"]) <= 5 and len(status["sessions"]) <= 16
    assert len(json.dumps(status)) < 20_000


# --- the read-only API -----------------------------------------------------------------------


@pytest.fixture
def live_api(client, tmp_path, monkeypatch, rollouts):
    c = cfg(tmp_path)
    cli.prepare(c)
    monkeypatch.setattr(api, "_workspace", lambda: Path(c.service.workspace))
    monkeypatch.setenv("LEVI_LIVE_HOME", c.service.home)
    return client, c


def test_the_api_answers_disabled_outside_a_live_workspace(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(api, "_workspace", lambda: tmp_path)
    for path in ("status", "sessions", "datasets", "audit"):
        assert client.get(f"/api/levi/live/{path}").json() == {
            "enabled": False,
            "reason": "not_live",
        }


def test_the_api_merges_sessions_health_progress_and_faults(
    live_api, rollouts, tmp_path, request
):
    client, c = live_api
    rollouts.write(0)
    rollouts.write(1)
    rollouts.incomplete(1, "fr3_fault")
    rollouts.session("fault", reason="joint reflex")
    health = tmp_path / "health.json"
    health.write_text(
        json.dumps(
            {"updated_at_epoch": time.time(), "red_light": True, "reasons": ["x"]}
        )
    )
    c.fr3.health_file = str(health)
    # No vLLM on the port, no worker: this test reads the status, and on a
    # machine with a vLLM on :8100 the default port would label for real.
    c.vllm.port = free_port()
    (c.live_dir / "effective.toml").write_text(live_config.render(c))
    spawned = []
    ctl = controller.Controller(
        c, popen=lambda *a, **k: spawned.append(a) or 1 / 0, log=lambda *a: None
    )
    request.addfinalizer(ctl.shutdown)  # after the status was read
    ctl.state = "idle"
    ctl.tick()
    ctl.write_status()
    assert spawned == []
    status = client.get("/api/levi/live/status").json()
    assert status["enabled"] and status["alive"] and status["fr3_red"]
    assert status["service"]["schema"].startswith("levi.live.status")
    (fault,) = status["faults"]
    assert fault["dataset"] == "pi05_fake__stack_the_plates"
    assert any("session" in r for r in fault["reasons"]) and any(
        "FR3" in r for r in fault["reasons"]
    )
    live = client.get("/api/levi/live/sessions").json()
    assert live["sessions"][0]["state"] == "fault" and live["sessions"][0]["fault"]
    assert live["sessions"][0]["dataset"] == "pi05_fake__stack_the_plates"
    assert live["fr3"]["state"] == "red"
    name = "pi05_fake__stack_the_plates"
    assert (
        client.get("/api/levi/live/datasets").json()["datasets"][name]["pending"] == 1
    )
    detail = client.get(f"/api/levi/live/datasets/{name}").json()
    assert detail["task_text"] == "" or detail["task_text"]  # filled once mirrored
    assert detail["review"] == "auto" and detail["evaluated"] is False
    assert detail["review_runs"] == []
    assert client.get("/api/levi/live/datasets/nope").status_code == 404
    assert client.get("/api/levi/live/datasets/..%2Fx").status_code == 404


def test_a_dead_service_is_reported_not_alive_and_the_api_reveals_no_secret(live_api):
    client, c = live_api
    status = {
        "schema": "levi.live.status.v1",
        "pid": 2**22 + 99,
        "updated_at": time.time(),
        "state": "idle",
        "datasets": {},
    }
    mirror.jsonio.write(c.status_file, status)
    answer = client.get("/api/levi/live/status").json()
    assert answer["enabled"] and answer["alive"] is False
    for path in ("status", "sessions", "datasets", "audit"):
        text = client.get(f"/api/levi/live/{path}").text.lower()
        assert "human.key" not in text and "token" not in text


def test_the_status_counts_the_runs_the_gate_stopped(live_api):
    from levi.live import resumer

    client, c = live_api
    mirror.jsonio.write(
        c.status_file,
        {
            "schema": "levi.live.status.v1",
            "pid": 1,
            "updated_at": time.time(),
            "state": "idle",
            "datasets": {},
        },
    )
    none = client.get("/api/levi/live/status").json()["blocked_runs"]
    assert none == {"count": 0, "waiting": [], "needs_person": []}
    mirror.jsonio.write(
        c.live_dir / resumer.FILE,
        {
            "updated_at": time.time(),
            "runs": [
                {"id": "run-a", "auto": True, "why": None},
                {"id": "run-b", "auto": False, "why": "given_up"},
            ],
        },
    )
    shown = client.get("/api/levi/live/status").json()["blocked_runs"]
    assert shown == {"count": 2, "waiting": ["run-a"], "needs_person": ["run-b"]}


# --- resources --------------------------------------------------------------------------------


def run_python(code, **env):
    full = {**os.environ, "PYTHONPATH": with_netguard(str(PROJECT)), **env}
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=full,
        cwd=PROJECT,
        check=False,
    )


def test_a_process_lowers_its_priority_and_caps_its_threads():
    done = run_python(
        "import os, json\n"
        "from levi.live import config, resources\n"
        "r = resources.apply(config.Config())\n"
        "print(json.dumps([os.getpriority(os.PRIO_PROCESS, 0), os.environ['OMP_NUM_THREADS'],"
        " os.environ['OPENCV_FOR_THREADS_NUM'], r]))"
    )
    assert done.returncode == 0, done.stderr
    nice, omp, cv, report = json.loads(done.stdout)
    assert nice == 19 and omp == "2" and cv == "2" and report["nice"] == 19
    if report["ionice"] is not None:  # idle class where the machine allows it
        assert report["ionice"] == 3


def test_the_idle_supervisor_loads_no_numerical_library():
    done = run_python(
        "import sys\n"
        "import levi.live.controller, levi.live.cli, levi.live.mirror, levi.live.gpumgr\n"
        "heavy = [m for m in ('pandas','numpy','cv2','pydantic','fastapi','pyarrow','torch') if m in sys.modules]\n"
        "print(heavy)"
    )
    assert done.stdout.strip() == "[]", done.stdout + done.stderr


def test_logs_rotate_and_the_run_cache_is_capped(tmp_path):
    log = resources.rotating_logger("t", tmp_path / "x.log", max_mb=0.001, backups=2)
    for n in range(400):
        log.info("line %s %s", n, "x" * 50)
    files = sorted(p.name for p in tmp_path.glob("x.log*"))
    assert files == ["x.log", "x.log.1", "x.log.2"]
    big = tmp_path / "c.log"
    big.write_bytes(b"0" * 3000)
    resources.rotate_file(big, max_mb=0.001, backups=2)
    assert not big.exists() and (tmp_path / "c.log.1").exists()
    base = tmp_path / "workbench/agent/datasets/d/runs"
    for n, age in enumerate((9 * 3600, 8 * 3600, 7 * 3600)):
        for part in ("input", "evidence"):
            folder = base / f"r{n}" / part
            folder.mkdir(parents=True)
            (folder / "f").write_bytes(b"0" * 1000)
            os.utime(folder, (time.time() - age, time.time() - age))
    result = resources.trim_cache(tmp_path / "workbench", 3500 / 1024**3, keep=["r2"])
    assert result["removed"] >= 1 and result["after"] <= 3500
    assert (base / "r2" / "input").exists()  # the batch in progress stays
    assert not (base / "r0" / "input").exists()  # the oldest went first


# --- the service as a process -----------------------------------------------------------------


def free_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def outside():
    """A workspace for a `levi live` child process. The child protects every
    `.state` of the checkout it runs from, and pytest's temporary folders lie
    under this checkout's `.state` (in-process tests point the guard at a
    scratch checkout instead, see conftest.py): so outside the checkout."""
    import shutil

    base = short_dir()
    yield base / "ws"
    shutil.rmtree(base, ignore_errors=True)


def service(tmp_path, *args, ws=None):
    return [
        sys.executable,
        "-m",
        "levi.live",
        *args,
        "--workspace",
        str(ws or tmp_path / "ws"),
        "--home",
        str(tmp_path / "home"),
        "--root",
        str(tmp_path / "rollouts"),
        # Never the default :8100 (a real model server may be there).
        "--vllm-port",
        str(free_port()),
        "--gpu-mode",
        "manual",
    ]


def cli_run(tmp_path, *args, timeout=120, ws=None):
    env = {**os.environ, "PYTHONPATH": with_netguard(str(PROJECT))}
    return subprocess.run(
        service(tmp_path, *args, ws=ws),
        capture_output=True,
        text=True,
        env=env,
        cwd=PROJECT,
        timeout=scaled(timeout),
        check=False,
    )


def test_the_service_idles_cheaply_heartbeats_and_stops_on_request(
    tmp_path, rollouts, outside
):
    home = tmp_path / "home"
    started = cli_run(tmp_path, "start", "--daemon", "--no-core", "--no-ui", ws=outside)
    assert started.returncode == 0, started.stdout + started.stderr
    try:
        status_file = home / "status.json"
        status = json.loads(status_file.read_text())
        pid = status["pid"]
        assert status["state"] == "idle" and status["accepts_sessions"]
        assert client_accepts(status, rollouts.root, time.time())
        # Idle for ten seconds: how much CPU, memory and how many threads.
        before = resources.cpu_seconds(pid)
        stamps = set()
        for _ in range(10):
            time.sleep(1.0)
            stamps.add(json.loads(status_file.read_text())["updated_at"])
        used = resources.cpu_seconds(pid) - before
        assert used < 0.6, f"idle supervisor used {used:.2f} s of CPU in 10 s"
        assert resources.rss_mb(pid) < 80
        assert resources.thread_count(pid) <= 4
        assert len(stamps) >= 2  # the heartbeat moves (<= 5 s apart)
        assert os.getpriority(os.PRIO_PROCESS, pid) == 19
        again = cli_run(tmp_path, "start", "--no-core", "--no-ui", ws=outside)
        assert again.returncode == 1 and "already running" in again.stdout
        shown = cli_run(tmp_path, "status", ws=outside)
        assert shown.returncode == 0 and "state      idle" in shown.stdout
        doctor = json.loads(cli_run(tmp_path, "doctor", "--json", ws=outside).stdout)
        assert doctor["alive"] and doctor["processes"][0]["nice"] == 19
        assert not [w for w in doctor["warnings"] if "supervisor holds" in w]
        assert doctor["processes"][0]["fds"] > 0 and doctor["processes"][0]["fd_limit"]
        assert not [w for w in doctor["warnings"] if "open files" in w]
    finally:
        stopped = cli_run(tmp_path, "stop", timeout=200, ws=outside)
    assert stopped.returncode == 0 and "stopped" in stopped.stdout
    assert json.loads((home / "status.json").read_text())["state"] == "stopped"
    assert cli_run(tmp_path, "stop", ws=outside).stdout.startswith(
        "The live service is not running"
    )
    assert cli_run(tmp_path, "status", ws=outside).returncode == 3


def test_once_with_the_fake_model_labels_everything_and_exits(
    tmp_path, rollouts, outside
):
    rollouts.write(0)
    rollouts.write(1)
    rollouts.begin(2)
    done = cli_run(
        tmp_path,
        "once",
        "--fake-vlm",
        "--auto-approve",
        "--process-backlog",
        timeout=300,
        ws=outside,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "pi05_fake__stack_the_plates: 0 / 0 / 2 / 0 / 1" in done.stdout
    audit = (outside / "live/audit.jsonl").read_text()
    assert "changes.commit" in audit and "refused" not in audit


def test_doctor_warns_when_vllm_is_failing_and_resume_clears_the_wait(tmp_path):
    c = cfg(tmp_path)
    cli.prepare(c)
    status = {
        "schema": "levi.live.status.v1",
        "pid": os.getpid(),
        "updated_at": time.time(),
        "state": "gpu_wait",
        "gpu": {"vllm": {"state": "error", "error": "ValueError: 1.82 GiB KV cache"}},
        "attention": {"code": "vllm_failed", "reason": "ValueError: 1.82 GiB KV cache"},
        "datasets": {},
    }
    mirror.jsonio.write(c.status_file, status)
    mirror.jsonio.write(
        c.home / "live.pid",
        {"pid": os.getpid(), "identity": controller.gpumgr.identity(os.getpid())},
    )
    report = cli.diagnose(c)
    warning = next(w for w in report["warnings"] if "failing to start" in w)
    assert "KV cache" in warning and "levi live resume" in warning
    # Exited children are not listed with empty numbers.
    assert all(p["rss_mb"] is not None for p in report["processes"])
    done = cli_run(tmp_path, "resume")
    assert done.returncode == 0 and "KV cache" in done.stdout
    assert (tmp_path / "ws/live/resume.json").exists()


def _idle_status(c, **gpu):
    now = time.time()
    status = {
        "schema": "levi.live.status.v1",
        "pid": os.getpid(),
        "updated_at": now,
        "state": "idle",
        "gpu": {
            "vllm": {"state": "ready", "started_at": now - 5000},
            "idle_since": now - c.vllm.idle_timeout_s - 500,
            **gpu,
        },
        "datasets": {},
    }
    mirror.jsonio.write(c.status_file, status)
    mirror.jsonio.write(
        c.home / "live.pid",
        {"pid": os.getpid(), "identity": controller.gpumgr.identity(os.getpid())},
    )


def test_doctor_says_a_vllm_nobody_uses_should_have_been_released(tmp_path):
    c = cfg(tmp_path)
    cli.prepare(c)
    _idle_status(c)
    assert any("nothing to do" in w for w in cli.diagnose(c)["warnings"])


def test_doctor_does_not_call_a_prewarmed_or_a_loading_or_a_woken_vllm_idle(tmp_path):
    c = cfg(tmp_path)
    cli.prepare(c)
    # Prewarmed on the command line (the file's own setting is still false) ...
    _idle_status(c, prewarm=True)
    assert not any("nothing to do" in w for w in cli.diagnose(c)["warnings"])
    # ... prewarmed in the file ...
    c.vllm.prewarm = True
    _idle_status(c)
    assert not any("nothing to do" in w for w in cli.diagnose(c)["warnings"])
    c.vllm.prewarm = False
    # ... still loading (no idle clock), however long ago it was launched ...
    _idle_status(c, idle_since=None, vllm={"state": "starting", "started_at": 1.0})
    assert not any("nothing to do" in w for w in cli.diagnose(c)["warnings"])
    # ... and one that was woken a moment ago: its start time is long past, its
    # idle time is not.
    _idle_status(c, idle_since=time.time() - 5)
    assert not any("nothing to do" in w for w in cli.diagnose(c)["warnings"])


def test_doctor_warns_when_the_loop_has_stopped_ticking_though_the_status_is_fresh(
    tmp_path,
):
    """The heartbeat thread keeps ``updated_at`` fresh even if the main loop is
    stuck; ``loop_at`` (the last tick) is what gives it away."""
    c = cfg(tmp_path)
    cli.prepare(c)
    now = time.time()
    status = {
        "schema": "levi.live.status.v1",
        "pid": os.getpid(),
        "updated_at": now,
        "loop_at": now - 900,
        "state": "active",
        "gpu": {},
        "datasets": {},
    }
    mirror.jsonio.write(c.status_file, status)
    mirror.jsonio.write(
        c.home / "live.pid",
        {"pid": os.getpid(), "identity": controller.gpumgr.identity(os.getpid())},
    )
    warnings = cli.diagnose(c)["warnings"]
    assert any("not ticked" in w and "900" in w for w in warnings), warnings
    mirror.jsonio.write(c.status_file, {**status, "loop_at": now - 2})
    assert not any("not ticked" in w for w in cli.diagnose(c)["warnings"])
    # A status from before loop_at existed says nothing.
    status.pop("loop_at")
    mirror.jsonio.write(c.status_file, status)
    assert not any("not ticked" in w for w in cli.diagnose(c)["warnings"])


def test_the_controller_writes_the_time_of_its_last_tick(tmp_path):
    c = cfg(tmp_path)
    cli.prepare(c)
    ctl = controller.Controller(c, log=lambda *a: None)
    try:
        ctl.tick(1000.0)
        assert json.loads(c.status_file.read_text())["loop_at"] == 1000.0
        assert ctl.status(1005.0)["loop_at"] == 1000.0
    finally:
        ctl.shutdown()


# --- the page and the core must really be up -----------------------------------------


def short_dir():
    """A workspace short enough for the core's Unix socket (the repo's own
    pytest base temp is not)."""
    import tempfile

    return Path(tempfile.mkdtemp(prefix="lvt", dir="/tmp"))


def live_cmd(workspace, home, *args):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "levi.live",
            *args,
            "--workspace",
            str(workspace),
            "--home",
            str(home),
            # Nothing of the machine's real rollouts directory or model server.
            "--root",
            str(Path(home).parent / "rollouts"),
            "--vllm-port",
            str(free_port()),
            "--gpu-mode",
            "manual",
        ],
        capture_output=True,
        text=True,
        cwd=PROJECT,
        env={**os.environ, "PYTHONPATH": with_netguard(str(PROJECT))},
        timeout=scaled(240),
        check=False,
    )


def test_a_workspace_path_too_long_for_the_core_socket_is_refused_up_front(tmp_path):
    c = cfg(tmp_path)
    c.service.workspace = str(tmp_path / ("w" * 90))
    with pytest.raises(ValueError, match="Unix socket"):
        cli.prepare(c, core=True)
    done = live_cmd(tmp_path / ("w" * 90), tmp_path / "home2", "start", "--daemon")
    assert done.returncode == 2 and "too long" in done.stderr
    assert not (tmp_path / "home2/status.json").exists()  # nothing was started


def test_a_daemon_whose_core_cannot_start_says_so_instead_of_claiming_success():
    import shutil
    import socket

    base = short_dir()
    try:
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            started = live_cmd(
                base / "ws",
                base / "home",
                "start",
                "--daemon",
                "--no-ui",
                "--core-port",
                str(port),
            )
            try:
                assert started.returncode == 2, started.stdout + started.stderr
                assert "did not come up" in started.stdout
                assert "Core failed to start" in started.stdout
                status = json.loads((base / "home/status.json").read_text())
                assert status["frontend"]["error"] and status["accepts_sessions"]
            finally:
                live_cmd(base / "ws", base / "home", "stop")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_the_watchdog_gives_up_after_three_restarts_and_says_so_once(tmp_path):
    front = cli.Frontend.__new__(cli.Frontend)
    front.config, front.log, front.process, front.ui = cfg(tmp_path), print, None, False
    front.state, front.attempts, front.started, front.error = "ok", 0, 0.0, ""
    starts = []
    front.start = lambda ui=False: (
        starts.append(1),
        setattr(front, "state", "starting"),
    )
    front._alive = lambda: False
    front._tail_error = lambda: "RuntimeError: Core failed to start"
    messages = []
    now = 1000.0
    for _ in range(12):
        now += 400  # past every back-off
        front.state = "ok" if front.state == "starting" else front.state
        message = front.check(now)
        if message:
            messages.append(message)
    assert len(starts) == 3  # three restarts, then it stops
    assert len(messages) == 4 and "giving up" in messages[-1]
    assert "Core failed to start" in messages[-1]
    assert front.state == "failed" and front.check(now + 9999) is None


# --- the workspace guard and the per-workspace lock --------------------------------------


def test_the_service_refuses_a_workspace_that_is_not_a_live_one(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    c.service.workspace = str(tmp_path / "ws")
    # The product LEVI's workspace: LEVI state, no live marker.
    (tmp_path / "ws/outputs/LEVI/workbench").mkdir(parents=True)
    with pytest.raises(ValueError, match="not a live one"):
        cli.prepare(c)
    assert not (tmp_path / "ws/live/workspace.json").exists()  # nothing written
    assert not (tmp_path / "ws/live.toml").exists()
    cli.prepare(c, adopt=True)  # explicit, and then it is a live workspace
    assert (tmp_path / "ws/live/workspace.json").exists()
    cli.prepare(c)  # now fine without the flag
    # The checkout's own .state is refused whatever the flag says.
    checkout = tmp_path / "checkout"
    (checkout / ".state").mkdir(parents=True)
    monkeypatch.setattr(locate, "checkout_root", lambda: checkout)
    for inside in (checkout / ".state", checkout / ".state/tmp/ws", checkout):
        own = cfg(tmp_path)
        own.service.workspace = str(inside)
        with pytest.raises(ValueError, match="product LEVI"):
            cli.prepare(own, adopt=True)


def test_the_main_checkout_and_the_environment_workspace_are_refused_even_adopted(
    tmp_path, monkeypatch
):
    """Run from a worktree, the product checkout's .state is another path; and
    LEVI_WORKSPACE names whichever workspace the shell's LEVI uses. Neither
    may become the live workspace, whatever --adopt-workspace says."""
    main = tmp_path / "LEVI"
    (main / ".git/worktrees/live-fix").mkdir(parents=True)
    tree = tmp_path / "LEVI-live-fix"
    tree.mkdir()
    (tree / ".git").write_text(f"gitdir: {main}/.git/worktrees/live-fix\n")
    monkeypatch.setattr(locate, "checkout_root", lambda: tree)
    c = cfg(tmp_path)
    c.service.workspace = str(main / ".state")
    (main / ".state/outputs/LEVI").mkdir(parents=True)
    with pytest.raises(ValueError, match="product LEVI"):
        cli.prepare(c, adopt=True)
    assert not (main / ".state/live").exists()
    # The shell's own LEVI workspace.
    mine = tmp_path / "somebodys-ws"
    (mine / "outputs/LEVI").mkdir(parents=True)
    monkeypatch.setenv("LEVI_WORKSPACE", str(mine))
    c.service.workspace = str(mine)
    with pytest.raises(ValueError, match="LEVI_WORKSPACE"):
        cli.prepare(c, adopt=True)
    # Once it is a live workspace (made on purpose, with the variable unset),
    # the variable naming it is no reason to refuse.
    monkeypatch.delenv("LEVI_WORKSPACE")
    cli.prepare(c, adopt=True)
    monkeypatch.setenv("LEVI_WORKSPACE", str(mine))
    cli.prepare(c)


def test_init_and_start_respect_the_guard(tmp_path, outside):
    (outside / "outputs/LEVI").mkdir(parents=True)
    done = cli_run(tmp_path, "init", ws=outside)
    assert done.returncode == 2 and "not a live one" in done.stderr
    assert cli_run(tmp_path, "start", "--no-core", ws=outside).returncode == 2
    # And a child process protects this checkout's `.state` by itself.
    inside = cli_run(tmp_path, "init")
    assert inside.returncode == 2 and "product LEVI" in inside.stderr


def test_two_homes_cannot_run_on_one_workspace(tmp_path):
    ws = tmp_path / "ws"
    first = controller.Instance(tmp_path / "home1", ws)
    second = controller.Instance(tmp_path / "home2", ws)
    other_ws = controller.Instance(tmp_path / "home3", tmp_path / "ws2")
    same_home = controller.Instance(tmp_path / "home1", tmp_path / "ws3")
    assert first.acquire()
    assert not second.acquire()  # a different home, the same workspace
    assert not same_home.acquire()  # the same home, another workspace
    assert other_ws.acquire()
    first.release()
    assert second.acquire()
    for item in (second, other_ws):
        item.release()


# --- the heartbeat, the idle probe, --fake-vlm and the page's fields -------------------


def test_the_status_heartbeat_continues_while_the_loop_is_busy(tmp_path):
    """Stopping a worker or vLLM can block a tick for a minute; a client that
    finds status.json stale falls back to manual labelling."""
    c = cfg(tmp_path)
    c.service.heartbeat_s = 0.5
    c.vllm.port = free_port()
    ctl = controller.Controller(
        c,
        probes=controller.Probes(
            vram=lambda: None, ports=set, holders=list, policy_vram=lambda p: None
        ),
        log=lambda *a: None,
    )
    ctl.hooks.append(lambda now: time.sleep(4.0))  # a tick that blocks for 4 s
    thread = threading.Thread(target=ctl.run, kwargs={"max_seconds": 6})
    thread.start()
    stamps = set()
    try:
        deadline = time.time() + 4.0
        while time.time() < deadline:
            try:
                stamps.add(json.loads(c.status_file.read_text())["updated_at"])
            except (OSError, ValueError):
                pass
            time.sleep(0.2)
    finally:
        ctl.running = False
        ctl.wake.set()
        thread.join(30)
        ctl.shutdown()
    assert len(stamps) >= 4, f"status.json moved {len(stamps)} times in a blocked tick"


def test_an_idle_tick_opens_no_connection_to_the_model_port(tmp_path):
    from levi.live import fakevlm

    server, fake, port = fakevlm.serve(0)
    try:
        c = cfg(tmp_path)
        c.vllm.port = port
        ctl = controller.Controller(
            c,
            probes=controller.Probes(
                vram=lambda: None, ports=set, holders=list, policy_vram=lambda p: None
            ),
            log=lambda *a: None,
        )
        for n in range(6):
            ctl.tick(time.time() + n)
        assert fake.health_checks == 0  # nothing to label: :8100 is not touched
    finally:
        server.shutdown()


def test_fake_vlm_refuses_the_live_workspace(tmp_path):
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "levi.live",
            "once",
            "--fake-vlm",
            "--auto-approve",
            "--home",
            str(tmp_path / "home"),
        ],
        capture_output=True,
        text=True,
        cwd=PROJECT,
        env={
            **{k: v for k, v in os.environ.items() if k != "LEVI_LIVE_WORKSPACE"},
            "PYTHONPATH": with_netguard(str(PROJECT)),
        },
        check=False,
    )
    assert done.returncode == 2 and "scratch" in done.stderr
    assert not (tmp_path / "home/status.json").exists()


def test_the_page_gets_the_reset_countdown_and_open_review_count(
    live_api, rollouts, request
):
    client, c = live_api
    rollouts.write(0)
    rollouts.session("waiting_reset")
    c.vllm.port = free_port()  # never the machine's vLLM on :8100
    spawned = []
    ctl = controller.Controller(
        c,
        probes=controller.Probes(
            vram=lambda: None, ports=set, holders=list, policy_vram=lambda p: None
        ),
        popen=lambda *a, **k: spawned.append(a) or 1 / 0,
        log=lambda *a: None,
    )
    request.addfinalizer(ctl.shutdown)
    ctl.tick()
    ctl.write_status()
    assert spawned == []
    status = json.loads(c.status_file.read_text())
    row = status["sessions"][0]
    assert row["reset_wait_s"] == 10.0 and row["waiting_reset_since"] is not None
    answer = client.get("/api/levi/live/sessions").json()["sessions"][0]
    assert answer["reset_wait_s"] == 10.0
    assert answer["waiting_reset_since"] == row["waiting_reset_since"]
    assert status["datasets"]["pi05_fake__stack_the_plates"]["review_runs_open"] == 0


def test_a_pid_record_without_an_identity_is_no_running_service(tmp_path):
    c = cfg(tmp_path)
    instance = controller.Instance(c.home, c.workspace)
    c.home.mkdir(parents=True, exist_ok=True)
    mirror.jsonio.write(instance.pid_path, {"pid": os.getpid(), "identity": None})
    assert instance.holder() is None  # None == None must not match a live pid
    mirror.jsonio.write(
        instance.pid_path,
        {"pid": os.getpid(), "identity": controller.gpumgr.identity(os.getpid())},
    )
    assert instance.holder()["pid"] == os.getpid()
