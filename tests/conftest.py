import importlib.util
import json
import os
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest_plugins = ("pytester",)

# --- network guard -------------------------------------------------------------------
# On the maintainer's machine these loopback ports belong to services a person
# is using: the local vLLM (8100), the product LEVI (7860/7861), the live
# service (7880/7881) and the robot and policy servers (5000/8000). No test
# may talk to them; one that must (it never should) says so with
# ``@pytest.mark.allow_service_ports``. The guard is installed in this process
# and, through ``tests/netguard`` first on PYTHONPATH (as ``sitecustomize``),
# in every Python process a test starts.
PROTECTED_PORTS = (8100, 7860, 7861, 7880, 7881, 5000, 8000)
NETGUARD_DIR = Path(__file__).resolve().parent / "netguard"
_spec = importlib.util.spec_from_file_location(
    "levi_test_netguard", NETGUARD_DIR / "sitecustomize.py"
)
netguard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(netguard)
_NETGUARD_LOG_DIR = None
# A test may replace builtins.open (to prove code opens no file); the guard's
# own bookkeeping must not trip over that.
_open = open


def with_netguard(pythonpath: str = "") -> str:
    """A PYTHONPATH that keeps the guard for a child process."""
    parts = [str(NETGUARD_DIR)] + [p for p in pythonpath.split(os.pathsep) if p]
    return os.pathsep.join(dict.fromkeys(parts))


# Wall-clock budgets of the tests' wait helpers (how long to wait for a job,
# a worker or a server) are multiplied by LEVI_TEST_TIME_SCALE (default 1),
# so a slower machine such as a CI runner can give them more time.
def time_scale() -> float:
    value = float(os.environ.get("LEVI_TEST_TIME_SCALE") or 1)
    if value <= 0:
        raise ValueError("LEVI_TEST_TIME_SCALE must be positive")
    return value


def scaled(seconds: float) -> float:
    return seconds * time_scale()


def pytest_configure(config):
    global _NETGUARD_LOG_DIR
    config.addinivalue_line(
        "markers",
        "allow_service_ports: the test may connect to the protected loopback "
        "ports (8100, 7860, 7861, 7880, 7881, 5000, 8000)",
    )
    _NETGUARD_LOG_DIR = tempfile.mkdtemp(prefix="levi-netguard-")
    log = os.path.join(_NETGUARD_LOG_DIR, "blocked.jsonl")
    os.environ[netguard.PORTS_ENV] = ",".join(map(str, PROTECTED_PORTS))
    os.environ[netguard.LOG_ENV] = log
    os.environ["PYTHONPATH"] = with_netguard(os.environ.get("PYTHONPATH", ""))
    netguard.install(PROTECTED_PORTS, None)


def pytest_unconfigure(config):
    if _NETGUARD_LOG_DIR:
        shutil.rmtree(_NETGUARD_LOG_DIR, ignore_errors=True)


def _child_attempts(offset):
    log = os.environ.get(netguard.LOG_ENV, "")
    if _child_log_size() <= offset:
        return []
    try:
        with _open(log, encoding="utf-8") as source:
            source.seek(offset)
            lines = source.read().splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


def _child_log_size():
    try:
        return os.path.getsize(os.environ.get(netguard.LOG_ENV, ""))
    except OSError:
        return 0


@pytest.fixture(autouse=True)
def _no_protected_ports(request):
    """Fail a test that tried to connect to a protected port (in this process
    or a child); the attempt itself was refused."""
    guard = netguard.GUARD
    allowed = request.node.get_closest_marker("allow_service_ports") is not None
    guard.enabled = not allowed
    start, offset = len(guard.blocked), _child_log_size()
    yield guard
    guard.enabled = True
    attempts = guard.blocked[start:] + _child_attempts(offset)
    if attempts and not allowed:
        pytest.fail(f"connected to a protected service port: {attempts}", pytrace=False)


@pytest.fixture
def dataset(tmp_path):
    root = tmp_path / "fixture"
    (root / "meta").mkdir(parents=True)
    (root / "data/chunk-000").mkdir(parents=True)
    info = {
        "codebase_version": "v2.1",
        "robot_type": "so100_follower",
        "fps": 10,
        "total_episodes": 2,
        "total_frames": 40,
        "total_tasks": 1,
        "chunks_size": 1000,
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": {
            "action": {
                "dtype": "float32",
                "shape": [2],
                "names": ["shoulder_pan.pos", "gripper.pos"],
            },
            "observation.state": {
                "dtype": "float32",
                "shape": [2],
                "names": ["shoulder_pan.pos", "gripper.pos"],
            },
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
        },
    }
    (root / "meta/info.json").write_text(json.dumps(info))
    (root / "meta/tasks.jsonl").write_text(
        json.dumps({"task_index": 0, "task": "Move the gripper"}) + "\n"
    )
    (root / "meta/episodes.jsonl").write_text(
        "\n".join(
            json.dumps(
                {"episode_index": ep, "length": 20, "tasks": ["Move the gripper"]}
            )
            for ep in range(2)
        )
        + "\n"
    )
    for ep in range(2):
        t = np.arange(20, dtype=float) / 10
        pd.DataFrame(
            {
                "action": [[float(np.sin(x)), float(x)] for x in t],
                "observation.state": [
                    [float(np.sin(x - 0.1)), float(x - 0.1)] for x in t
                ],
                "timestamp": t,
                "episode_index": [ep] * 20,
                "frame_index": range(20),
                "index": range(ep * 20, (ep + 1) * 20),
                "task_index": [0] * 20,
            }
        ).to_parquet(root / f"data/chunk-000/episode_{ep:06d}.parquet")
    return root


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import backend.app as annotations
    from levi import catalog, jobs, service

    state = tmp_path / "outputs"
    monkeypatch.setattr(catalog, "STATE", state)
    monkeypatch.setattr(service, "STATE", state)
    monkeypatch.setattr(jobs, "STATE", state)
    # Auto-named conversion job outputs land directly under this module's
    # own ROOT (see jobs.plan's explicit base=ROOT) — without this, a plain
    # "/api/levi/jobs/plan" call with no custom output writes into the real,
    # live LEVI_WORKSPACE (.state/levi_<job id>/) instead of the test's own
    # tmp_path, leaking real directories on every test run.
    monkeypatch.setattr(jobs, "ROOT", tmp_path)
    monkeypatch.setattr(annotations, "STATE", state)
    monkeypatch.setattr(annotations, "EXPORT_ROOT", tmp_path / "exports")
    # Keep boundary validation enabled; test temp dirs live under the workspace.
    annotations._states.clear()
    yield TestClient(service.app)
    # A job thread still running after the test would write through the
    # module paths once the monkeypatches above are undone -- into the real
    # workspace. Wait for them while the test's paths are still in place.
    assert not jobs.wait_idle(scaled(120)), "a job thread outlived its test"


@pytest.fixture(autouse=True)
def _no_droid_sample(monkeypatch, tmp_path):
    """No test downloads the DROID sample a new workspace draws, and none
    reads or resumes the live workspace's sample ledger."""
    from levi.samples import droid

    monkeypatch.setenv("LEVI_DROID_SAMPLE", "off")
    monkeypatch.setattr(droid, "ROOT", tmp_path)
    monkeypatch.setattr(droid, "STATE", tmp_path / "outputs/LEVI/workbench")


@pytest.fixture(autouse=True)
def _no_real_live_service(monkeypatch, tmp_path):
    """The live page of any LEVI looks for the live service's status file
    (``levi/live/locate.py``): never this machine's ``~/.levi-live`` or a
    ``LEVI_LIVE_WORKSPACE`` from the shell. A test that wants one sets it."""
    monkeypatch.setenv("LEVI_LIVE_HOME", str(tmp_path / "no-live-home"))
    monkeypatch.delenv("LEVI_LIVE_WORKSPACE", raising=False)
    # The `.state` of every checkout is refused as (or around) a live
    # workspace, and the test's temporary folders lie under this checkout's
    # `.state`: in this process the protected checkout is a scratch one. A
    # test of the guard points it at a checkout of its own; a `levi live`
    # child process protects the real one and gets a workspace outside it
    # (`outside` in test_live_service.py).
    from levi.live import locate

    monkeypatch.setattr(locate, "checkout_root", lambda: tmp_path / "no-checkout")


@pytest.fixture(autouse=True)
def _gpu_is_not_this_machines(monkeypatch):
    """The off-peak GPU guard reads the real nvidia-smi; a test must not pass
    or fail depending on who is training on this machine right now."""
    monkeypatch.setenv("LEVI_GPU_SHARING", "allow")


@pytest.fixture(autouse=True)
def _workspace_files_stay_in_the_test(monkeypatch, tmp_path):
    """GPU history, learned request costs, evaluation records and the process
    registry live under the live workspace by default; a test must never add
    to them (or reclaim the real service's workers)."""
    from levi import children
    from levi.eval import record
    from levi.inference import gpu, request_cost

    # The process registry decides what the service kills at start and stop.
    monkeypatch.setattr(children, "_path", lambda: tmp_path / "processes.json")

    monkeypatch.setattr(gpu, "_state_dir", lambda: tmp_path / "models")
    monkeypatch.setattr(request_cost, "_state_dir", lambda: tmp_path / "models")
    monkeypatch.setattr(record, "eval_dir", lambda: tmp_path / "eval")


@pytest.fixture(autouse=True)
def fresh_gpu_verdict():
    """A GPU "free" reused across tests would hide the next test's GPU."""
    from levi.inference import gpu

    gpu._RECENT_FREE.clear()
    yield
    gpu._RECENT_FREE.clear()


@pytest.fixture(scope="session")
def demo_template(tmp_path_factory):
    """One fake rollout demo (built once: ffmpeg is the slow part) for the
    live-service tests to copy."""
    from live_helpers import template

    return template(tmp_path_factory)
