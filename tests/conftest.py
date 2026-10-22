import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest_plugins = ("pytester",)

# --- network guard -------------------------------------------------------------------
# On the maintainer's machine these loopback ports belong to services a person
# is using: the local vLLM (8100), the product LEVI (7860/7861), the live
# service (7880/7881) and the robot and policy servers (5000/8000). No test
# may talk to them on any address of this machine. The guard is installed in
# this process and, through ``tests/netguard`` first on PYTHONPATH (as
# ``sitecustomize``), in every Python process a test starts. A test that must
# reach them (none should) says so with ``@pytest.mark.allow_service_ports``:
# that lifts the guard in this process, and LEVI_TEST_NETGUARD_OFF=1 is set
# while it runs so the Python processes it starts are not guarded either.
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


# On GitHub Actions the failed tests' names go out as error annotations, the one
# part of a run that can be read without signing in (scripts/ci_annotations.py).
_ci_spec = importlib.util.spec_from_file_location(
    "levi_ci_annotations",
    Path(__file__).resolve().parent.parent / "scripts" / "ci_annotations.py",
)
ci_annotations = importlib.util.module_from_spec(_ci_spec)
_ci_spec.loader.exec_module(ci_annotations)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    failures = []
    for kind in ("failed", "error"):
        for report in terminalreporter.stats.get(kind, []):
            nodeid = getattr(report, "nodeid", "") or "(collection)"
            crash = getattr(getattr(report, "longrepr", None), "reprcrash", None)
            message = getattr(crash, "message", None) or str(
                getattr(report, "longrepr", "") or ""
            )
            failures.append((kind, nodeid, message))
    for line in ci_annotations.lines(failures):
        terminalreporter.write_line(line)


def pytest_configure(config):
    global _NETGUARD_LOG_DIR
    config.addinivalue_line(
        "markers",
        "allow_service_ports: the test may connect to this machine's protected "
        "service ports (8100, 7860, 7861, 7880, 7881, 5000, 8000); lifts the "
        "guard in the pytest process, and sets LEVI_TEST_NETGUARD_OFF=1 while "
        "the test runs so the Python processes it starts are unguarded too",
    )
    config.addinivalue_line(
        "markers",
        "mode_matrix(*capabilities, modes=None): the test covers these AERI "
        "MODE_MATRIX capabilities (levi/automatic/modes.py) in the mode of its "
        "reset_mode parameter, or in the modes it names with modes=(...)",
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


def _new_live_threads(before):
    return [t for t in threading.enumerate() if t not in before and t.is_alive()]


@pytest.fixture(autouse=True)
def _no_leaked_threads():
    """Fail the test that leaves a thread running after its own teardown.

    A thread that outlives its test is not harmless: ``levi.live.owned_runtime``
    refuses an idle ack while any other thread lives in the process
    ("model still has local request threads"), so a leaked recorder, server or
    Starlette portal thread makes unrelated tests in tests/live fail later in
    the same pytest run. Fix the leaking test, not the check: close the
    ``TestClient`` with ``with``, call the fake's ``close()``/``shutdown()``, or
    join the thread in a fixture finalizer. This fixture is set up before the
    test's other function fixtures, so it is torn down after them.
    """
    before = set(threading.enumerate())
    # Read the scale before the test runs: a test may set LEVI_TEST_TIME_SCALE
    # to an invalid value, and its monkeypatch is undone only after this
    # fixture's teardown.
    budget = scaled(2)
    yield
    deadline = time.monotonic() + budget
    leaked = _new_live_threads(before)
    while leaked and time.monotonic() < deadline:
        for thread in leaked:
            thread.join(timeout=0.05)
        leaked = _new_live_threads(before)
    if leaked:
        lines = "\n".join(
            f"  {t.name!r} daemon={t.daemon} target={getattr(t, '_target', None)!r}"
            for t in leaked
        )
        pytest.fail(
            f"left {len(leaked)} thread(s) running after teardown:\n{lines}",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _close_fake_recorders(monkeypatch, _no_leaked_threads):
    """Close every ``FakeRecorder`` a test builds, closed by the test or not.

    ``FakeRecorder`` (integrations/fr3_automatic/fake.py, loaded by
    tests/automatic/aeri_harness.py as ``fr3_automatic_fake``) starts a
    ``fake-recorder`` writer thread when built. The AERI rigs build many, and
    ``rig(recorder={...})`` builds one itself, so tests cannot all close theirs;
    an open one lives to the end of the pytest process. ``close()`` is
    idempotent. Requesting ``_no_leaked_threads`` makes pytest tear this one
    down first (autouse fixtures are otherwise ordered by name), so the
    recorders are closed before that guard looks.
    """
    fake = sys.modules.get("fr3_automatic_fake")
    built = []
    before = set(threading.enumerate())
    if fake is not None:
        original = fake.FakeRecorder.__init__

        def init(self, *args, **kwargs):
            original(self, *args, **kwargs)
            built.append(self)

        monkeypatch.setattr(fake.FakeRecorder, "__init__", init)
    yield
    for recorder in built:
        recorder.close()
    # The module was first imported during this test (a file run on its
    # own), so __init__ was not wrapped: close the recorders by their threads.
    for thread in set(threading.enumerate()) - before:
        owner = getattr(getattr(thread, "_target", None), "__self__", None)
        if thread.name == "fake-recorder" and hasattr(owner, "close"):
            owner.close()


@pytest.fixture(autouse=True)
def _no_protected_ports(request):
    """Fail a test that tried to connect to a protected port (in this process
    or a child); the attempt itself was refused."""
    guard = netguard.GUARD
    allowed = request.node.get_closest_marker("allow_service_ports") is not None
    off = os.environ.get(netguard.OFF_ENV)
    guard.enabled = not allowed
    if allowed:
        os.environ[netguard.OFF_ENV] = "1"
    start, offset = len(guard.blocked), _child_log_size()
    try:
        yield guard
    finally:
        guard.enabled = True
        if allowed:
            if off is None:
                os.environ.pop(netguard.OFF_ENV, None)
            else:
                os.environ[netguard.OFF_ENV] = off
    attempts = guard.blocked[start:] + _child_attempts(offset)
    if attempts and not allowed:
        lines = "\n".join(
            f"  {a['host']}:{a['port']} from pid {a['pid']}"
            + ("" if a["pid"] == os.getpid() else f" argv {a.get('argv')}")
            for a in attempts
        )
        pytest.fail(
            f"connected to a protected service port (refused):\n{lines}",
            pytrace=False,
        )


@pytest.fixture(params=("single_reset_policy", "human_assisted"))
def reset_mode(request):
    """Each AERI reset mode in turn (levi/automatic/modes.RESET_MODES):
    with a ``mode_matrix`` marker the test covers its capabilities in both."""
    return request.param


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


def pytest_sessionstart(session):
    """Refuse to run in a checkout whose ``.state`` belongs to a running LEVI.

    The tests read and write the checkout's default workspace in places (``maintenance.STATE`` and the pool's
    remembered workspaces are fixed when the modules are imported), so in the maintainer's checkout a run
    saw the product's own state (18 failures and errors on 2026-10-07) and could touch it. Run the suite
    from a worktree (``git worktree add lab/worktrees/<topic> -b <branch> main``) or after ``levi stop``;
    ``LEVI_TESTS_IN_PRODUCT=1`` overrides it for someone who knows better."""
    if os.environ.get("LEVI_TESTS_IN_PRODUCT"):
        return
    from levi import maintenance

    marker = maintenance.STATE / "server.pid"
    try:
        pid = int(marker.read_text())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return
    pytest.exit(
        f"This checkout's workspace {maintenance.STATE.parent.parent.parent} is in use by a running LEVI "
        f"(pid {pid}). Run the tests from a git worktree (git worktree add lab/worktrees/<topic> -b <branch> main) "
        "or stop LEVI first (levi stop); LEVI_TESTS_IN_PRODUCT=1 overrides.",
        returncode=3,
    )


@pytest.fixture(autouse=True)
def _ignore_the_developers_dotenv(monkeypatch):
    """``levi.paths`` loads the checkout's ``.env`` into ``os.environ`` when it is imported, so a test run in
    a checkout that has one (the maintainer's, with the pool roots, the GPU lock file, the report folder, the
    linked workspaces of a running product) saw those settings: with the live service running, the segmentation
    tests were refused by its GPU lock and the links tests found its workspace. CI and a fresh clone have no
    ``.env``; this makes every checkout behave like them. ``LEVI_WORKSPACE`` stays (the runner may set it)."""
    from dotenv import dotenv_values

    env_file = Path(__file__).resolve().parents[1] / ".env"
    if env_file.is_file():
        for key in dotenv_values(env_file):
            if key != "LEVI_WORKSPACE":
                monkeypatch.delenv(key, raising=False)


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
    # A reuse window exported in the shell would change the guard's own tests.
    monkeypatch.delenv("LEVI_GPU_SHARED_REUSE_SECONDS", raising=False)


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
