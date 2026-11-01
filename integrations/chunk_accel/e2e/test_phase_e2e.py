"""End to end for chunk acceleration v3 (the phase clock): the REAL client on FAKE hardware (design section 7, WP3, G-I).

What runs: the user's real collector and client (examples/fr3_local, loaded read-only from the openpi checkout, in the
openpi virtualenv) with run_collect_phase.py's hooks, real websocket to e2e/fake_policy_server.py, real HTTP to
e2e/lag_robot_server.py (an arm that lags its target by 0.27 s, a 2.65 Hz mode, a gripper that moves 0.2 s after its
command), fake cameras (franka_control/tests/live/fake_camera.py) and scripted keys (tests_live/harness.py, imported by
path). Nothing here touches a real port: the servers bind 127.0.0.1 on 5071-5079 / 8041-8049, picked by binding.

Three layers, cheapest first:
  * client_check / install: the AST check and the installer on the real tree and on mutated copies (stdlib, no server);
  * the entry's hooks on stub client modules (header, trace, summary, close / abort, refusals) with the real engine;
  * subprocess runs: 3 modes x 2 episodes x 60 steps at lambda 2.5 and at the page default 3.0, and the failure cases.

Run (pytest of the LEVI worktree venv; the client subprocesses use the openpi venv):
    OMP_NUM_THREADS=1 nice -n 19 taskset -c 8-11 .venv/bin/python -m pytest -q integrations/chunk_accel/e2e \
        --basetemp=.state/tmp/pt-wp3 -p no:cacheprovider
The client the subprocesses run is this file:  python test_phase_e2e.py client <client flags...>
"""

import hashlib
import importlib.util
import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).absolute().parent
CA = HERE.parent
# The openpi checkout and franka_control/tests/live come from the environment, with no developer default (a machine path
# in a shipped file fails tests/test_live_portable.py); the tests that need them skip when they are unset.
OPENPI = Path(
    os.environ.get("CHUNK_ACCEL_OPENPI_ROOT") or "chunk-accel-openpi-root-not-set"
).absolute()
OPENPI_PY = OPENPI / ".venv/bin/python"
HARNESS = OPENPI / "examples/fr3_local/tests_live/harness.py"
FAKES = Path(os.environ.get("CHUNK_ACCEL_FAKES_DIR") or "chunk-accel-fakes-dir-not-set")
FORBIDDEN_PORTS = (5000, 5001, 5100, 7470, 8000, 8010)
PROMPT = "stack the plates of same color together"
HOME = (
    0.45,
    0.0,
    0.18,
    3.119139,
    0.008288,
    -0.883962,
)  # the start of lag_robot_server and fake_policy_server
MODES = ("plain", "rtc", "vlash")


# ============================================================================== the client side (openpi interpreter)


def install_fake_cameras():
    """Fake cameras for the CURRENT fr3_env: the user's franka_control/tests/live/fake_camera.py predates its
    read_frame / FrameRing API (AttributeError: 'FakeCamera' object has no attribute 'read_frame'), so the harness's
    install is replaced by this one. Every read is a new frame (identical frames are what the stall detectors look for)."""
    import types as _types
    import zlib

    if "pyrealsense2" not in sys.modules:
        sys.modules["pyrealsense2"] = _types.ModuleType("pyrealsense2")
    import fr3_env

    class PhaseFakeCamera:
        def __init__(self, name, config):
            self.name, self.config, self.n = name, config, 0
            self.ring = fr3_env.FrameRing(config.ring_capacity)
            self.rng = np.random.default_rng(zlib.crc32(name.encode()))

        def read_frame(self, timeout=5.0):
            self.n += 1
            w, h = self.config.dim
            image = self.rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)
            image[:, (self.n * 3) % w] = 255
            return self.ring.push(image, None, time.monotonic_ns())

        def read(self):
            return self.read_frame().image

        def close(self):
            pass

    fr3_env._RealSenseCamera = PhaseFakeCamera


def client_main(argv):
    """Runs the entry under the harness: the collector's tyro class and main are swapped for the entry's, so the
    harness's own keyboard script, fake cameras and port refusal apply unchanged. PHASE_E2E_ENTRY_DIR names the
    directory run_collect_phase.py is imported from (default: this source tree; an installed lab/chunk_accel for the
    install test, where the openpi root is then found from the file's own location)."""
    entry_dir = os.environ.get("PHASE_E2E_ENTRY_DIR")
    if entry_dir is None:
        os.environ.setdefault("CHUNK_ACCEL_OPENPI_ROOT", str(OPENPI))
    sys.path.insert(0, entry_dir or str(CA))
    fault = json.loads(os.environ.get("PHASE_E2E_FAULT") or "{}")
    import run_collect_phase as entry  # puts examples/fr3_local on sys.path and imports the collector

    if "engine_raise_at_commit" in fault:
        at, calls, real_commit = (
            int(fault["engine_raise_at_commit"]),
            [0],
            entry.phase.PhaseEngine.commit,
        )

        def commit(self):
            calls[0] += 1
            if calls[0] >= at:
                raise RuntimeError("injected engine failure")
            return real_commit(self)

        entry.phase.PhaseEngine.commit = commit
    spec = importlib.util.spec_from_file_location(
        "phase_e2e_harness", os.environ.get("PHASE_E2E_HARNESS") or HARNESS
    )
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(
        harness
    )  # puts examples/fr3_local and franka_control/tests/live on sys.path
    import fake_camera

    fake_camera.install = install_fake_cameras  # what harness.main() calls
    cc = entry.collector
    real_main = cc.main

    def phase_main(args):
        cc.main = real_main  # the entry calls collector.main itself
        entry.main(args)

    cc.Args, cc.main = entry.Args, phase_main
    sys.argv = ["harness.py", *argv]
    harness.main()


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "client":
    client_main(sys.argv[2:])
    raise SystemExit(0)

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(CA))


# ============================================================================== helpers (pytest side)


def _have_openpi():
    return (
        OPENPI_PY.is_file()
        and HARNESS.is_file()
        and (FAKES / "fake_camera.py").is_file()
    )


needs_openpi = pytest.mark.skipif(
    not _have_openpi(),
    reason="set CHUNK_ACCEL_OPENPI_ROOT (an openpi checkout with its .venv) and CHUNK_ACCEL_FAKES_DIR "
    "(franka_control/tests/live)",
)
needs_tree = pytest.mark.skipif(
    not (OPENPI / "lab/vlash/runtime.py").is_file(),
    reason="set CHUNK_ACCEL_OPENPI_ROOT to an openpi checkout",
)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


class Rig:
    """A lag robot server (in this process, a thread) and a fake policy server (a subprocess in the openpi venv)."""

    def __init__(
        self, tmp, mode, overlap=2, latency="real", policy_args=(), robot_kwargs=None
    ):
        import fake_policy_server
        import lag_robot_server

        if lag_robot_server.LagRobotServer is None:
            pytest.skip(lag_robot_server.MISSING)
        self.tmp, self.mode, self.fake_policy_server = (
            Path(tmp),
            mode,
            fake_policy_server,
        )
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.robot_port = lag_robot_server.pick_port()
        self.policy_port = fake_policy_server.pick_port()
        assert not {self.robot_port, self.policy_port} & set(FORBIDDEN_PORTS)
        self.robot = lag_robot_server.LagRobotServer(
            self.robot_port, **(robot_kwargs or {})
        )
        self.policy_args = [
            "--port", str(self.policy_port), "--mode", mode, "--overlap-steps", str(overlap), "--latency", str(latency),
            *policy_args,
        ]  # fmt: skip
        self.proc = None

    def __enter__(self):
        self.robot.start()
        self.log_file = open(self.tmp / f"policy-{self.mode}.log", "wb")
        self.proc = subprocess.Popen(
            [str(OPENPI_PY), str(HERE / "fake_policy_server.py"), *self.policy_args],
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.time() + 30
        while self.fake_policy_server.bindable(
            self.policy_port
        ):  # listening = not bindable (never a connect probe)
            if self.proc.poll() is not None or time.time() > deadline:
                self.__exit__()
                raise AssertionError(
                    "fake policy server did not start:\n"
                    + (self.tmp / f"policy-{self.mode}.log").read_text()[-2000:]
                )
            time.sleep(0.1)
        return self

    def __exit__(self, *exc):
        self.robot.stop()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()  # our own PID, by PID (workspace R5)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.kill(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=10)
        self.log_file.close()
        assert self.fake_policy_server.bindable(self.policy_port), (
            "the policy server left its port open"
        )


def client_args(rig, tmp, *, mode=None, lam=2.5, alpha=0.005, steps=60, episodes=2, control_hz=10, rtc_flag=None,
                extra=(), levi=None):  # fmt: skip
    mode = mode or rig.mode
    rollout = Path(tmp) / "rollouts"
    rollout.mkdir(exist_ok=True)
    args = [
        "--remote-host", "127.0.0.1", "--remote-port", str(rig.policy_port),
        "--franka-server-url", f"http://127.0.0.1:{rig.robot_port}/",
        "--prompt", PROMPT, "--max-timesteps", str(steps), "--control-hz", str(control_hz),
        "--home-pose", *(str(v) for v in HOME), "--home-duration-s", "1.0",
        "--no-display-images", "--no-step-by-step", "--camera-width", "128", "--camera-height", "96",
        "--rollout-root", str(rollout), "--eval-dir", str(Path(tmp) / "eval_log"), "--eval-num", str(episodes),
        "--phase-mode", mode, "--phase-lambda-max", str(lam), "--phase-alpha", str(alpha),
    ]  # fmt: skip
    if mode == "plain":
        args += ["--open-loop-horizon", "6"]
    elif mode == "rtc":
        args += ["--rtc-execute-horizon", "5"]
    else:
        args += ["--vlash-overlap-steps", "2"]
    if rtc_flag if rtc_flag is not None else mode == "rtc":
        args += ["--rtc"]
    if (
        levi is not None
    ):  # unattended LEVI mode: nobody presses keys, the episode runs its whole budget
        args += ["--levi-mode", "on", "--levi-status-file", str(levi["status"]),
                 "--fr3-health-file", str(levi["health"]), "--reset-wait", "1.5"]  # fmt: skip
    return args + list(extra)


def manual_keys(steps, episodes, settle=1.5):
    """Start gate, steps-1 idle polls, success; between episodes a pause for the lagging arm to arrive at home."""
    one = ["\r"] + [None] * (steps - 1) + ["s"]
    out = []
    for i in range(episodes):
        out += one + ([["sleep", settle]] if i + 1 < episodes else [])
    return out


def run_client(args, tmp, *, keys=None, fault=None, entry_dir=None, timeout=170):
    env = dict(
        os.environ,
        PYTHONUNBUFFERED="1",
        no_proxy="localhost,127.0.0.1,::1",
        NO_PROXY="localhost,127.0.0.1,::1",
    )
    for name in (
        "HARNESS_KEYS",
        "PHASE_E2E_FAULT",
        "PHASE_E2E_ENTRY_DIR",
        "CHUNK_ACCEL_OPENPI_ROOT",
    ):
        env.pop(name, None)
    env["PHASE_E2E_HARNESS"] = str(
        HARNESS
    )  # the client process may not have the openpi root in its environment
    env["FAKE_CAMERA_FLAG_DIR"] = str(Path(tmp) / "camflags")
    (Path(tmp) / "camflags").mkdir(exist_ok=True)
    if entry_dir is not None:
        env["PHASE_E2E_ENTRY_DIR"] = str(
            entry_dir
        )  # the openpi root is then found from where the entry lives
    else:
        env["CHUNK_ACCEL_OPENPI_ROOT"] = str(OPENPI)
    if keys is not None:
        env["HARNESS_KEYS"] = json.dumps(keys)
    if fault:
        env["PHASE_E2E_FAULT"] = json.dumps(fault)
    cmd = [
        "timeout",
        "-k",
        "5",
        str(timeout),
        str(OPENPI_PY),
        str(Path(__file__).absolute()),
        "client",
        *args,
    ]
    proc = subprocess.Popen(cmd, cwd=str(tmp), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)  # fmt: skip
    try:
        out, _ = proc.communicate(timeout=timeout + 20)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        raise AssertionError("client timed out\n" + out[-3000:])
    return proc.returncode, out


def demos(tmp, prefix="demo_"):
    root = Path(tmp) / "rollouts"
    return {
        p.parent.name: json.loads(p.read_text())
        for p in sorted(root.rglob("metadata.json"))
        if p.parent.name.startswith(prefix)
    }


def eval_logs(tmp):
    return [
        json.loads(p.read_text())
        for p in sorted((Path(tmp) / "eval_log").rglob("*.json"))
    ]


def assert_trace(chunk_accel, *, ticks_at_least=20):
    """eval.chunk_accel's trace entry matches the file on disk; returns the parsed lines."""
    info = chunk_accel["trace"]
    path = Path(info["file"])
    assert (
        path.is_file()
        and path.parent.name.endswith(".phase")
        and path.name.startswith("ep_")
    )
    assert info["sha256"] == sha256(path), "the recorded sha256 is not the file's"
    lines = read_jsonl(path)
    assert info["lines"] == len(lines)
    kinds = {rec["kind"] for rec in lines}
    assert {"tick", "sent", "chunk", "end"} <= kinds, kinds
    assert lines[-1]["kind"] == "end"
    assert sum(1 for rec in lines if rec["kind"] == "tick") >= ticks_at_least
    return lines


def assert_envelope(lines, lam_max):
    """The design's invariants, recomputed from the trace with the client's own formula (rc:706-707), not the engine's.
    The step is measured from the last target the client EXECUTED (a refused one never left the client: its `sent`
    line says rejected)."""
    prev, last = None, None
    for rec in lines:
        if rec["kind"] == "sent" and last is not None:
            if not rec["rejected"]:
                prev = last
            continue
        if rec["kind"] != "tick":
            continue
        tgt, meas = (
            np.asarray(rec["tgt"], np.float64),
            np.asarray(rec["meas"], np.float64),
        )
        assert np.isfinite(tgt).all()
        assert rec["lam"] <= lam_max + 1e-9
        d_p = float(np.linalg.norm(tgt[:3] - meas[:3]))
        d_r = float(np.abs((tgt[3:6] - meas[3:6] + np.pi) % (2 * np.pi) - np.pi).max())
        if not rec["hold"]:
            assert d_p <= 0.08 + 1e-6 and d_r <= 0.28 + 1e-6, (rec["k"], d_p, d_r)  # I4
            if prev is not None:
                step_p = float(np.linalg.norm(tgt[:3] - prev[:3]))
                step_r = float(
                    np.abs((tgt[3:6] - prev[3:6] + np.pi) % (2 * np.pi) - np.pi).max()
                )
                assert step_p <= 0.05 + 1e-6 and step_r <= 0.12 + 1e-6, (
                    rec["k"],
                    step_p,
                    step_r,
                )  # I5
        last = tgt


def check_phase_run(tmp, rc, out, mode, *, episodes=2, lam=2.5, rig=None):
    assert rc == 0, out[-4000:]
    assert "Jump exceeds" not in out and "Traceback" not in out, out[-4000:]
    assert f"chunk acceleration v3 (phase clock): mode={mode} lambda_max={lam}" in out
    found = demos(tmp)
    assert len(found) == episodes, list(found)
    for name, m in found.items():
        assert m["eval"]["rejected_jumps"] == 0, name
        ca = m["eval"]["chunk_accel"]
        assert ca["rule"] == "v3" and ca["mode"] == mode and ca["ticks"] >= 20
        assert_envelope(assert_trace(ca), lam)
        header = m["policy"]["chunk_accel"]
        assert (
            header["rule"] == "v3"
            and header["mode"] == mode
            and header["lambda_max"] == lam
        )
        assert header["relaxed_tag"] == "RELAXED[2026-11-01]" and isinstance(
            header["code_drift"], bool
        )
        assert {
            "phase.py",
            "schedulers.py",
            "run_collect_phase.py",
            "rc",
            "rtcc",
            "coll",
            "vrt",
        } <= set(header["shas"])
        assert header["params"]["lambda_max"] == lam and "D_alpha" in header["derived"]
        assert header["trace_dir"] and Path(header["trace_dir"]).is_dir()
        if mode == "plain":
            assert (
                "rtc" not in m["eval"]
                and "rtc" not in m["policy"]
                and "vlash" not in m["eval"]
            )
        elif mode == "rtc":
            assert (
                m["policy"]["rtc"]["execute_horizon"] == 5
                and m["policy"]["rtc"]["server"]["version"] == 1
            )
            r = m["eval"]["rtc"]  # RtcChunker.summary()'s keys, plus the phase ones
            assert {
                "requests",
                "rtc_requests",
                "starved_ticks",
                "delays",
                "latency_ms_median",
            } <= set(r)
            assert {
                "delays_rows",
                "swap_anchor_rows",
                "blend_ticks",
                "phase_lambda_mean",
            } <= set(r)
            assert r["rtc_requests"] >= 1 and "vlash" not in m["eval"]
        else:
            assert m["policy"]["vlash"]["overlap_steps"] == 2
            v = m["eval"]["vlash"]
            assert v["adjusted_targets"] == 0, (
                v
            )  # I7: acceleration never trips action_adjusted
            assert v["requests"] >= 2 and "rtc" not in m["eval"]
    logs = eval_logs(tmp)
    assert (
        len(logs) == 1
        and logs[0]["chunk_accel"]["mode"] == mode
        and len(logs[0]["episodes"]) == episodes
    )
    if rig is not None:
        # The arm's own view of every commanded target. The leash and the client's 0.10 m check are against the pose the
        # tick OBSERVED; by the time /pose arrives the arm has moved on (plain: a synchronous ~85 ms inference in
        # between, about 22 mm at 0.26 m/s), so this physical figure may exceed 0.10 m a little. It is reported by
        # the e2e (measured 0.104 m, plain, straight) and bounded here; the invariants are the trace's.
        assert rig.robot.lag_stats()["max_target_err_m"] < 0.12
    return found


# ============================================================================== client_check and install (stdlib)


def _copy_client(dst):
    import client_check

    for rel in client_check.CLIENT_FILES.values():
        target = Path(dst) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(OPENPI / rel, target)
    return Path(dst)


@needs_tree
def test_the_real_client_passes_the_check_and_the_vlash_runtime_is_the_reviewed_one():
    import client_check

    shas = client_check.check_client(OPENPI)
    assert set(shas) == {"rc", "rtcc", "coll", "vrt"}
    assert shas["vrt"] == client_check.VRT_REVIEWED_SHA256


@needs_tree
@pytest.mark.parametrize(
    "name, old, new, needle",
    [
        ("rc", "def _rtc_chunker(", "def _rtc_chunker_x(", "_rtc_chunker is missing"),
        ("rc", "rtc.action_adjusted(", "rtc.action_adjusted_x(", "no longer calls rtc.action_adjusted"),
        ("rc", "recorder.stage(obs)", "recorder.stage_x(obs)", "no longer calls recorder.stage"),
        ("rc", "def _check_rtc_server(", "def _check_rtc_server_x(", "_check_rtc_server is missing"),
        ("rtcc", "self.period_s = ", "self.period_x = ", "no longer assigns _request, execute_horizon and period_s"),
        ("coll", "scheduler_factory=None, server_validator=None", "scheduler_factory=None", "lost scheduler_factory"),
        ("coll", "def stage(self", "def stage_x(self", "no longer has _session_metadata, stage"),
        ("vrt", "def check_server(", "def check_server_x(", "check_server is missing"),
    ],
)  # fmt: skip
def test_a_changed_client_is_refused_with_the_file_the_invariant_and_the_fix(
    tmp_path, name, old, new, needle
):
    import client_check

    root = _copy_client(tmp_path / "openpi")
    path = root / client_check.CLIENT_FILES[name]
    text = path.read_text()
    assert old in text, f"the test's anchor {old!r} is no longer in {name}"
    path.write_text(text.replace(old, new, 1))
    with pytest.raises(ValueError) as err:
        client_check.check_client(root)
    message = str(err.value)
    assert (
        needle in message
        and client_check.CLIENT_FILES[name] in message
        and "adapter" in message
    )


@needs_tree
def test_install_dry_run_lists_every_file_and_sha_and_writes_nothing(tmp_path):
    import client_check
    import install

    root = _copy_client(tmp_path / "openpi")
    result = install.install(root, apply=False)
    assert result["applied"] is False
    assert [r["name"] for r in result["files"]] == list(install.FILES)
    assert set(install.FILES) >= {
        "phase.py",
        "schedulers.py",
        "run_collect_phase.py",
        "accel.py",
        "run_collect_accel.py",
    }
    assert set(result["client"]) == {"rc", "rtcc", "coll", "vrt"}
    assert result["client"]["vrt"]["sha256"] == client_check.VRT_REVIEWED_SHA256
    assert result["vrt_matches_reviewed"] is True
    assert not (root / "lab/chunk_accel").exists()


@needs_tree
def test_install_apply_adds_files_keeps_v1_v2_bytes_writes_the_receipt_and_refuses_a_different_file(
    tmp_path,
):
    import install

    root = _copy_client(tmp_path / "openpi")
    dest = root / "lab/chunk_accel"
    dest.mkdir(parents=True)
    # an earlier install: v1/v2 only. Its bytes must come out of the new install unchanged.
    for name in ("accel.py", "run_collect_accel.py"):
        shutil.copyfile(CA / name, dest / name)
    before = {
        name: (dest / name).read_bytes()
        for name in ("accel.py", "run_collect_accel.py")
    }
    result = install.install(root, apply=True)
    assert result["applied"] is True
    for name in install.FILES:
        assert (dest / name).read_bytes() == (CA / name).read_bytes(), name
    assert {name: (dest / name).read_bytes() for name in before} == before
    receipt = json.loads((dest / install.RECEIPT).read_text())
    assert (
        receipt["schema"] == install.RECEIPT_SCHEMA
        and receipt["files"] == result["files"]
    )
    assert receipt["client"] == result["client"] and receipt["installed_iso"].endswith(
        "Z"
    )
    assert (
        install.install(root, apply=True)["files"] == result["files"]
    )  # again: nothing to add
    (dest / "phase.py").write_text(
        "# somebody's edit\n"
    )  # a different installed file is never overwritten
    with pytest.raises(
        ValueError, match="refusing to overwrite a different isolated adapter: phase.py"
    ):
        install.install(root, apply=True)
    link = tmp_path / "link"  # a symlink anywhere on the path is refused
    link.symlink_to(root)
    with pytest.raises(ValueError, match="symlink refused"):
        install.install(link, apply=False)


@needs_tree
def test_install_refuses_a_client_that_lacks_a_symbol(tmp_path):
    import install

    root = _copy_client(tmp_path / "openpi")
    path = root / "examples/fr3_local/run_robotiq_client.py"
    path.write_text(
        path.read_text().replace("def _rtc_chunker(", "def _rtc_chunker_x(", 1)
    )
    with pytest.raises(ValueError, match="_rtc_chunker is missing"):
        install.install(root, apply=False)
    assert not (root / "lab/chunk_accel").exists()


@needs_tree
def test_the_dry_run_on_the_real_tree_passes():
    """install.py --openpi-root <real tree> without --apply: the acceptance of WP3."""
    import install

    result = install.install(OPENPI, apply=False)
    assert result["applied"] is False
    assert {r["name"] for r in result["files"]} == set(install.FILES)
    assert not (OPENPI / "lab/chunk_accel" / install.RECEIPT).exists(), (
        "a dry run must not write the receipt"
    )


# ============================================================================== the entry's hooks on stub client modules


class StubWorld:
    """Stub client modules (rc, collector with RolloutRecorder / EvalLog / Args), the entry imported on top of them with
    the REAL engine and schedulers. One per test: the hooks are installed on these stubs and nowhere else."""

    START = np.array([0.45, 0.0, 0.18, 3.119139, 0.008288, -0.883962, 1.0], np.float32)

    def __init__(self, tmp, monkeypatch):
        import dataclasses

        self.tmp, self.notes, self.seen = Path(tmp), [], []
        self.closed, self.aborted, self.staged = [], [], []
        self.episode_kw = {}
        world = self

        class EvalLog:
            def __init__(self, path, args, prompt, meta=None):
                self.path = Path(path)
                self.header = {"schema": "fr3_local.eval_log.v1"}

        class Recorder:
            demo_dir = Path("demo_0007")

            def stage(self, obs):
                world.staged.append(obs)

            def _session_metadata(self):
                return {"policy": {"server": "x"}}

            def close(
                self,
                outcome,
                verdict_by,
                counted,
                stats,
                discard_if_invalid=False,
                extra=None,
            ):
                world.closed.append((outcome, extra))
                return "closed"

            def abort(self, reason, stats=None, detail=None):
                world.aborted.append((reason, detail))
                return "aborted"

        @dataclasses.dataclass
        class BaseArgs:
            open_loop_horizon: int = 6
            control_hz: float = 10.0
            rtc: bool = False
            rtc_execute_horizon: int = 5
            step_by_step: bool = False
            binarize_gripper: bool = True

        def clamp(action, current, args):
            world.seen.append(np.asarray(action, np.float32).copy())
            return np.asarray(action, np.float32).copy(), list(world.notes)

        self.rc = types.SimpleNamespace(
            _query_policy=self._query,
            _clamp_action=clamp,
            _run_episode=self._episode,
            _rtc_chunker=lambda client, args, prompt: None,
            EvalLog=EvalLog,
        )
        self.collector = types.SimpleNamespace(
            rc=self.rc,
            RolloutRecorder=Recorder,
            Args=BaseArgs,
            main=lambda args, **kw: None,
        )
        self.Recorder, self.EvalLog = Recorder, EvalLog
        monkeypatch.setattr(
            sys, "path", list(sys.path)
        )  # the entry puts the openpi tree on it; undone after the test
        monkeypatch.setitem(
            sys.modules, "run_robotiq_client_collect_data", self.collector
        )
        monkeypatch.setitem(
            sys.modules, "tyro", types.SimpleNamespace(cli=lambda cls: cls())
        )
        monkeypatch.delitem(sys.modules, "run_collect_phase", raising=False)
        monkeypatch.setenv("CHUNK_ACCEL_OPENPI_ROOT", str(OPENPI))
        spec = importlib.util.spec_from_file_location(
            "run_collect_phase", CA / "run_collect_phase.py"
        )
        self.entry = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, "run_collect_phase", self.entry)
        spec.loader.exec_module(self.entry)

    @staticmethod
    def _query(client, obs, state, prompt):
        """The plain policy: ten rows, 8 mm of +x each, gripper held."""
        rows = np.repeat(np.asarray(state, np.float32)[None], 10, axis=0)
        rows[:, 0] += 0.008 * np.arange(1, 11, dtype=np.float32)
        return rows

    def _episode(self, client, env, args, prompt, keys, display, recorder=None, **kw):
        """The client's plain loop (rc:1571-1657) in miniature: stage, query when the plan is used up, clamp, step."""
        rc, recorder = self.rc, recorder or self.Recorder()
        steps, fail_at = (
            self.episode_kw.get("steps", 30),
            self.episode_kw.get("fail_at"),
        )
        state, chunk, idx, sent = self.START.copy(), None, 0, 0
        for step in range(steps):
            if fail_at is not None and step == fail_at:
                raise RuntimeError("the loop broke")
            obs = {
                "robot_state": {"cartesian_position": state, "gripper_position": 1.0}
            }
            recorder.stage(obs)
            if chunk is None or idx >= min(6, len(chunk)):
                chunk, idx = rc._query_policy(client, obs, state, prompt), 0
            raw = chunk[idx]
            idx += 1
            action, notes = rc._clamp_action(raw, state, args)
            if any(str(n).startswith(("STEP", "ROT")) for n in notes):
                chunk = None
                continue
            state, sent = (
                action,
                sent + 1,
            )  # the stub arm sits exactly on the last target
        return "success", {"steps": sent}

    def args(self, mode="plain", **over):
        a = self.entry.Args()
        a.phase_mode = mode
        a.rtc = mode == "rtc"
        for key, value in over.items():
            setattr(a, key, value)
        return a

    def info(self):
        shas = {
            name: "0" * 64
            for name in (*self.entry.OWN_FILES, "rc", "rtcc", "coll", "vrt")
        }
        return {"shas": shas, "code_drift": True, "receipt": {"found": False, "drift": None, "vrt_reviewed": True},
                "root": str(OPENPI), "vrt_module": None}  # fmt: skip

    def install(self, mode="plain", **over):
        args = self.args(mode, **over)
        return args, self.entry.install(args, self.entry.validate(args), self.info())

    def eval_log(self):
        return self.rc.EvalLog(
            self.tmp / "eval_log" / "model" / "task_20261101_000000.json", None, "p"
        )

    def run(self, args):
        return self.rc._run_episode(
            None, None, args, "p", None, None, recorder=self.Recorder(), unattended=True
        )


@pytest.fixture
def world(tmp_path, monkeypatch):
    return StubWorld(tmp_path, monkeypatch)


def test_validate_refuses_what_the_design_refuses(world):
    entry = world.entry
    params = entry.validate(world.args("plain"))
    assert params.lambda_max == 2.0 and params.alpha == 0.005
    cases = [
        ({"control_hz": 20.0}, "--control-hz 10"),
        ({"step_by_step": True}, "--no-step-by-step"),
        ({"binarize_gripper": False}, "--binarize-gripper"),
        ({"phase_lambda_max": 0.9}, "--phase-lambda-max"),
        ({"phase_lambda_max": 3.5}, "--phase-lambda-max"),
        ({"phase_lambda_max": float("nan")}, "--phase-lambda-max"),
        ({"phase_alpha": 0.02}, "--phase-alpha"),
        ({"phase_mode": "warp"}, "--phase-mode"),
        ({"rtc": True}, "--phase-mode rtc needs --rtc"),  # plain with --rtc
        ({"open_loop_horizon": 0}, "--open-loop-horizon"),
    ]  # fmt: skip
    for over, needle in cases:
        with pytest.raises(ValueError) as err:
            entry.validate(world.args("plain", **over))
        assert needle in str(err.value), (over, str(err.value))
    with pytest.raises(
        ValueError, match="--phase-mode rtc needs --rtc"
    ):  # rtc without --rtc
        entry.validate(world.args("rtc", rtc=False))
    with pytest.raises(
        ValueError, match="--phase-mode rtc needs --rtc"
    ):  # vlash with --rtc
        entry.validate(world.args("vlash", rtc=True))
    with pytest.raises(ValueError, match="--rtc-execute-horizon"):
        entry.validate(world.args("rtc", rtc_execute_horizon=0))
    with pytest.raises(ValueError, match="--vlash-overlap-steps"):
        entry.validate(world.args("vlash", vlash_overlap_steps=0))
    for lam in (1.0, 1.5, 2.0, 2.5, 3.0):
        for alpha in (0.005, 0.01):
            assert (
                entry.validate(
                    world.args("plain", phase_lambda_max=lam, phase_alpha=alpha)
                ).alpha
                == alpha
            )


def test_args_default_to_plain_and_the_inference_mode_is_not_a_flag(world):
    import dataclasses

    a = world.entry.Args()
    assert (a.phase_mode, a.phase_lambda_max, a.phase_alpha, a.vlash_overlap_steps) == (
        "plain",
        2.0,
        0.005,
        2,
    )
    assert a.inference_mode == "plain"
    assert {f.name: f for f in dataclasses.fields(world.entry.Args)}[
        "inference_mode"
    ].init is False


def test_a_plain_episode_writes_header_trace_summary_and_hands_them_to_the_recorder(
    world,
):
    args, _run = world.install("plain", phase_lambda_max=2.5)
    log = world.eval_log()
    assert (
        log.header["chunk_accel"]["rule"] == "v3"
        and log.header["chunk_accel"]["mode"] == "plain"
    )
    assert log.header["chunk_accel"]["lambda_max"] == 2.5
    assert (
        world.Recorder()._session_metadata()["policy"]["chunk_accel"]["alpha"] == 0.005
    )
    json.dumps(log.header)  # the header is JSON-safe
    outcome, stats = world.run(args)
    assert outcome == "success" and stats["steps"] > 10
    world.Recorder().close("success", "key", True, stats, extra={"x": 1})
    _, extra = world.closed[0]
    ca = extra["chunk_accel"]
    assert (
        extra["x"] == 1
        and ca["rule"] == "v3"
        and ca["mode"] == "plain"
        and ca["outcome"] == "success"
    )
    assert ca["ticks"] >= 20 and ca["lambda_eff"] > 1.2, (
        "the engine's targets were not accelerated"
    )
    assert ca["trace"]["file"].endswith("task_20261101_000000.phase/ep_0001.jsonl")
    lines = assert_trace(ca)
    assert [rec["kind"] for rec in lines].count("sent") == ca["ticks"]
    assert lines[-1] == {"kind": "end", "demo": "demo_0007", "outcome": "success"}
    assert_envelope(lines, 2.5)
    json.dumps(ca)
    # the aperture of every tick reached the engine through the stage hook
    assert all(rec["aperture"] == 1.0 for rec in lines if rec["kind"] == "tick")
    # the summary goes to one close only; the next episode gets its own file
    world.Recorder().close("success", "key", True, stats)
    assert "chunk_accel" not in (world.closed[1][1] or {})
    world.run(args)
    world.Recorder().close("success", "key", True, {})
    assert world.closed[2][1]["chunk_accel"]["trace"]["file"].endswith("ep_0002.jsonl")


def test_the_clamp_sees_the_engines_targets_not_the_placeholder_rows(world):
    args, _run = world.install("plain", phase_lambda_max=2.5)
    world.eval_log()
    world.run(args)
    xs = [row[0] for row in world.seen]
    assert len(xs) >= 20
    # the policy's own rows advance 8 mm per tick; accelerated, the engine's targets advance clearly faster
    assert (xs[-1] - xs[0]) / (len(xs) - 1) > 0.010


def test_only_a_jump_note_counts_as_a_refusal(world):
    args, run = world.install("plain")
    world.eval_log()
    world.notes[:] = ["z 0.050->0.053"]  # a workspace clamp is not a refusal
    world.run(args)
    assert run.take_summary()["rejected"] == 0
    world.notes[:] = ["STEP 0.120m > 0.100m"]
    world.run(args)
    assert run.take_summary()["rejected"] >= 1


def test_a_failed_episode_leaves_a_flushed_trace_and_an_abort_detail_with_the_sha(
    world,
):
    world.episode_kw = {"steps": 30, "fail_at": 12}
    args, _run = world.install("plain")
    world.eval_log()
    with pytest.raises(RuntimeError, match="the loop broke"):
        world.run(args)
    world.Recorder().abort(
        "exception:RuntimeError", {"steps": 12}, {"error": "the loop broke"}
    )
    reason, detail = world.aborted[0]
    assert reason == "exception:RuntimeError" and detail["error"] == "the loop broke"
    ca = detail["chunk_accel"]
    assert ca["outcome"] == "exception:RuntimeError" and ca["mode"] == "plain"
    lines = assert_trace(ca, ticks_at_least=8)
    assert lines[-1]["outcome"] == "exception:RuntimeError"
    assert ca["trace"]["sha256"] == sha256(ca["trace"]["file"])


def test_an_episode_without_an_eval_log_is_refused_before_it_starts(world):
    args, run = world.install("plain")
    with pytest.raises(RuntimeError, match="no EvalLog was created"):
        world.run(args)
    assert run.trace is None and not world.seen


def test_the_aperture_capture_is_a_passthrough(world):
    _args, run = world.install("plain")
    obs = {"robot_state": {"cartesian_position": world.START, "gripper_position": 0.42}}
    world.Recorder().stage(obs)
    assert run.aperture == 0.42 and world.staged == [obs]
    world.Recorder().stage({"robot_state": {}})
    assert run.aperture is None and len(world.staged) == 2


def test_each_mode_installs_only_its_own_hooks(world):
    query, rtc_chunker = world.rc._query_policy, world.rc._rtc_chunker
    args, run = world.install("rtc")
    assert (
        world.rc._query_policy is query and world.rc._rtc_chunker is not rtc_chunker
    )  # hook C, not hook A
    built = types.SimpleNamespace(engine=object())
    wrapped = run.wrap_factory(lambda client, a, prompt: built)
    assert wrapped(None, args, "p") is built and run.sched is built
    with pytest.raises(RuntimeError, match="no .engine"):
        run.wrap_factory(lambda client, a, prompt: object())(None, args, "p")


def test_plain_is_stamped_with_the_observation_time_not_the_time_of_the_hook(
    world, monkeypatch
):
    """The staged hook (rc:1575, right after env.get_observation) takes the tick's time; hook A and hook B pass it on.
    On a query tick both looks then carry the time of the observation although the inference ran in between, so the
    next tick is 0.1 s later in the engine's eyes (its speed, the close gate's speed test, D_meas)."""
    import time as _time

    calls = []
    scheduler = world.entry.schedulers.PlainPhase
    real_q, real_a = scheduler.on_query, scheduler.on_action

    def on_query(self, chunk, state, aperture=None, t=None):
        calls.append(("query", t, _time.monotonic()))
        return real_q(self, chunk, state, aperture, t)

    def on_action(self, current, aperture=None, t=None):
        calls.append(("action", t, _time.monotonic()))
        return real_a(self, current, aperture, t)

    monkeypatch.setattr(scheduler, "on_query", on_query)
    monkeypatch.setattr(scheduler, "on_action", on_action)
    real_query = world._query

    def slow_query(client, obs, state, prompt):
        _time.sleep(0.03)  # the inference
        return real_query(client, obs, state, prompt)

    world.rc._query_policy = slow_query
    args, _run = world.install("plain")
    world.eval_log()
    world.episode_kw["steps"] = 14
    assert world.run(args)[0] == "success"
    assert calls and all(t is not None for _kind, t, _now in calls), calls
    stamps = []
    for kind, t, now in calls:
        if kind == "action":
            stamps.append(t)
    assert stamps == sorted(set(stamps)), "one strictly increasing time stamp per tick"
    queries = [(t, now) for kind, t, now in calls if kind == "query"]
    assert len(queries) >= 2
    for t, now in queries:
        assert now - t >= 0.025, "a query tick is stamped before its inference"
    for (kind_a, t_a, _), (kind_b, t_b, _) in itertools.pairwise(calls):
        if kind_a == "query":
            assert kind_b == "action" and t_a == t_b, (
                "both looks of a query tick share the time"
            )


def test_every_scheduler_gets_the_clients_workspace_predicate_with_the_live_args(world):
    """I7: the engine shrinks a blend offset until the CLIENT's clamp (min_z of this task, apply_task_min_z) does not
    touch it. The launcher used to leave set_workspace uncalled, so on a task with a z floor every plain splice and RTC
    swap could blend a target below it: a clamp the acceleration caused (G-D wants 0) and, in VLASH, an
    action_adjusted + reset + synchronous query."""

    def floor_clamp(action, current, args):
        action = np.asarray(action, np.float32).copy()
        notes = []
        if args.min_z > 0 and action[2] < args.min_z:
            notes.append(f"z {action[2]:.3f}->{args.min_z:.3f}")
            action[2] = args.min_z
        return action, notes

    world.rc._clamp_action = floor_clamp
    args, run = world.install("plain", min_z=0.075)
    world.eval_log()
    run.begin_episode()  # plain: registers its PlainPhase
    hit = run.engine._workspace_hit
    assert hit is not None, "set_workspace was not called"
    below, above = (
        np.array([0.45, 0, 0.0728, 3.1, 0, -0.9, 1], np.float32),
        np.array([0.45, 0, 0.0755, 3.1, 0, -0.9, 1], np.float32),
    )
    assert hit(below) is True and hit(above) is False
    assert run.ticks == 0 and world.seen == [], (
        "the predicate must call the client's own clamp, not hook B"
    )
    args.min_z = 0.05  # apply_task_min_z for another task, after the engine was built
    assert hit(below) is False
    args.min_z = 0.0  # no floor
    assert hit(below) is False
    # RTC and VLASH schedulers are registered through the same call
    from phase import PhaseEngine, PhaseParams

    for sched_engine in (PhaseEngine(PhaseParams()), PhaseEngine(PhaseParams())):
        args.min_z = 0.075
        run.register(types.SimpleNamespace(engine=sched_engine))
        assert sched_engine._workspace_hit(below) is True
        assert sched_engine._workspace_hit(above) is False
        args.min_z = 0.05
        assert sched_engine._workspace_hit(below) is False


def test_hook_a_is_installed_for_plain_only(tmp_path, monkeypatch):
    plain = StubWorld(tmp_path / "a", monkeypatch)
    before = plain.rc._query_policy, plain.rc._rtc_chunker
    plain.install("plain")
    assert (
        plain.rc._query_policy is not before[0] and plain.rc._rtc_chunker is before[1]
    )


@needs_tree
def test_an_rtc_episode_runs_on_stub_modules_through_hook_c_and_hook_b(world):
    """The real PhaseRtcChunker and engine, the user's rtc_chunker._Request (read-only), driven like rc's loop."""
    import schedulers

    rtcc = schedulers.load_rtc_module(OPENPI / "examples/fr3_local/rtc_chunker.py")

    def request(obs, prev, delay):
        st = np.asarray(obs["robot_state"]["cartesian_position"], np.float32)
        return lambda: (world._query(None, obs, st, "p"), {})

    def original(client, a, prompt):
        return types.SimpleNamespace(
            _request=request, execute_horizon=4, period_s=0.1, _Request=rtcc._Request,
            MAX_SEED_DELAY=rtcc.MAX_SEED_DELAY, WAIT_LIMIT_S=rtcc.WAIT_LIMIT_S,
        )  # fmt: skip

    def loop(client, env, a, prompt, keys, display, recorder=None, **kw):
        rtc = world.rc._rtc_chunker(client, a, prompt)
        state = world.START.copy()
        try:
            for _ in range(40):
                obs = {
                    "robot_state": {
                        "cartesian_position": state,
                        "gripper_position": 1.0,
                    }
                }
                recorder.stage(obs)
                action, _ = world.rc._clamp_action(rtc.next_action(obs), state, a)
                state = action
        finally:
            rtc.close()
        return "success", {"steps": 40}

    world.rc._rtc_chunker, world.rc._run_episode = original, loop
    args, _run = world.install("rtc", phase_lambda_max=2.0)
    world.eval_log()
    assert world.run(args)[0] == "success"
    world.Recorder().close("success", "key", True, {})
    ca = world.closed[0][1]["chunk_accel"]
    assert ca["mode"] == "rtc" and ca["ticks"] == 40
    lines = assert_trace(ca, ticks_at_least=40)
    assert any(rec["kind"] == "request" for rec in lines), (
        "the scheduler's request lines were not drained into the trace"
    )
    assert_envelope(lines, 2.0)


# ============================================================================== subprocess end to end (G-I)


@needs_openpi
@pytest.mark.parametrize("lam", (2.5, 3.0))
@pytest.mark.parametrize("mode", MODES)
def test_two_episodes_of_sixty_steps(tmp_path, mode, lam):
    """lambda 2.5 and the page default 3.0 (review 11-02: nothing ran the shipped value end to end)."""
    from phase import PhaseParams

    steps, episodes = 60, 2
    with Rig(tmp_path, mode, overlap=2) as rig:
        rc, out = run_client(client_args(rig, tmp_path, steps=steps, episodes=episodes, lam=lam), tmp_path,
                             keys=manual_keys(steps, episodes))  # fmt: skip
        found = check_phase_run(
            tmp_path, rc, out, mode, episodes=episodes, lam=lam, rig=rig
        )
        assert (
            "close" in rig.robot.lag_stats()["grip_commands"]
        )  # the script's grasp happened (and the lag model ran)
    for m in found.values():
        # the close gate's wait is bounded however often the plan is reloaded (review 11-02 M2: a plain re-query restarted
        # it and one close slipped by 33 ticks): 0.5 s of wait, plus one tick that may hold a slow plain query (<= 0.3 s).
        # And it is spent at the close it is for: the script's re-query moves the close 40-75 mm, and a wait carried
        # over from the old place sent both plain closes 43-45 mm from the close row (review of the 11-02 fixes).
        for ev in m["eval"]["chunk_accel"]["grip_events"]:
            if ev["kind"] == "close":
                assert ev["gate_wait_ticks"] <= 5 and ev["gate_wait_s"] <= 0.8, ev
                assert ev["arm_err_m"] <= 2 * PhaseParams().gate_dist_m, ev
        # per-tick wall time: the engine plus the loop fit the 100 ms tick. The tail is the fake server's latency
        # (up to 261 ms) inside a plain tick, so only the median and a generous p99 are held.
        gaps = np.diff(
            [
                rec["t"]
                for rec in read_jsonl(m["eval"]["chunk_accel"]["trace"]["file"])
                if rec["kind"] == "tick"
            ]
        )
        assert np.median(gaps) < 0.12 and np.percentile(gaps, 99) < 0.45, (
            np.median(gaps),
            np.percentile(gaps, 99),
        )


@needs_openpi
@pytest.mark.parametrize("mode", MODES)
def test_a_straight_run_is_hurried_in_every_mode(tmp_path, mode):
    """The pick-and-place script is all corners and dwells; on a straight line the engine has room, and the run says
    so (the speed that counts is the offline gates' and the arm's, this only shows the clock really advances)."""
    steps = 50
    with Rig(tmp_path, mode, policy_args=["--script", "straight"]) as rig:
        rc, out = run_client(
            client_args(rig, tmp_path, steps=steps, episodes=1),
            tmp_path,
            keys=manual_keys(steps, 1),
        )
        (m,) = check_phase_run(tmp_path, rc, out, mode, episodes=1, rig=rig).values()
    ca = m["eval"]["chunk_accel"]
    assert (
        ca["lambda_eff"] > 1.1
        and ca["lambda_p90"] > 1.4
        and ca["frac_ticks_lambda_gt_1.1"] > 0.3
    ), ca
    assert (
        ca["rows_advanced"] - ca["ticks"] > 4
    )  # rows saved, i.e. ticks saved at the policy's own rate


@needs_openpi
def test_rtc_v3_against_a_plain_server_is_refused_before_anything_moves(tmp_path):
    with Rig(tmp_path, "plain") as rig:
        args = client_args(
            rig, tmp_path, mode="rtc", steps=10, episodes=1
        )  # --rtc, but the server is plain
        rc, out = run_client(args, tmp_path, keys=["\r", "s"])
        assert rc != 0 and "does not advertise RTC" in out, out[-2500:]
        assert not list((tmp_path / "rollouts").rglob("metadata.json"))
        assert rig.robot.stats()["counts"].get("pose", 0) == 0


@needs_openpi
def test_servers_and_clients_of_the_wrong_kind_are_refused_before_anything_moves(
    tmp_path,
):
    with Rig(tmp_path / "v", "vlash") as rig:  # a VLASH server wants the VLASH client
        rc, out = run_client(
            client_args(rig, tmp_path / "v", mode="rtc", steps=10, episodes=1),
            tmp_path / "v",
            keys=["\r", "s"],
        )
        assert rc != 0 and "VLASH server requires the VLASH client" in out, out[-2500:]
        assert rig.robot.stats()["counts"].get("pose", 0) == 0
    with Rig(
        tmp_path / "p", "plain"
    ) as rig:  # and the VLASH client wants a VLASH server
        rc, out = run_client(
            client_args(rig, tmp_path / "p", mode="vlash", steps=10, episodes=1),
            tmp_path / "p",
            keys=["\r", "s"],
        )
        assert rc != 0 and "VLASH requires the reviewed" in out, out[-2500:]
        assert rig.robot.stats()["counts"].get("pose", 0) == 0


@needs_openpi
@pytest.mark.parametrize(
    "kwargs, needle",
    [
        ({"control_hz": 20}, "--control-hz 10"),
        ({"lam": 3.5}, "--phase-lambda-max"),
        ({"alpha": 0.02}, "--phase-alpha"),
        ({"extra": ["--no-binarize-gripper"]}, "--binarize-gripper"),
        ({"mode": "rtc", "rtc_flag": False}, "--phase-mode rtc needs --rtc"),
    ],
)  # fmt: skip
def test_the_entry_refuses_a_bad_start_before_it_connects_to_anything(
    tmp_path, kwargs, needle
):
    ports = types.SimpleNamespace(
        robot_port=5071, policy_port=8041, mode="plain"
    )  # nothing listens there
    rc, out = run_client(
        client_args(ports, tmp_path, steps=10, episodes=1, **kwargs),
        tmp_path,
        keys=["\r", "s"],
    )
    assert rc != 0 and needle in out, out[-2000:]
    assert "Connecting to policy server" not in out


# The page's own argv for the three phase forms (tests/marvel0/fixtures/chunk_accel_argv.json, written by the page's
# command builder, design section 6) through THIS entry's tyro parser and validate(), in the openpi interpreter that
# runs the entry. Nothing connects anywhere: parsing and validation only.
_PARSE_PAGE_ARGV = """
import importlib.util, json, sys
from pathlib import Path
ca, fixture, root = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(ca))
spec = importlib.util.spec_from_file_location("run_collect_phase", ca / "run_collect_phase.py")
entry = importlib.util.module_from_spec(spec)
sys.modules["run_collect_phase"] = entry
spec.loader.exec_module(entry)
import tyro
data = json.loads(fixture.read_text())
found = {}
for key in ("v3_plain", "v3_rtc", "v3_vlash"):
    argv = data[key]["argv"]
    args = tyro.cli(entry.Args, args=[a.replace("{root}", root) for a in argv[argv.index("lab/chunk_accel/run_collect_phase.py") + 1:]])
    params = entry.validate(args)
    found[key] = dict(mode=args.phase_mode, lam=args.phase_lambda_max, alpha=args.phase_alpha, rtc=args.rtc,
                      rtc_execute_horizon=args.rtc_execute_horizon, open_loop_horizon=args.open_loop_horizon,
                      overlap=args.vlash_overlap_steps, hz=args.control_hz, inference_mode=args.inference_mode,
                      step_by_step=args.step_by_step, binarize=args.binarize_gripper,
                      params=[params.lambda_max, params.alpha, params.golden])
print("PAGE_ARGV " + json.dumps(found))
"""


@needs_openpi
def test_the_pages_argv_for_each_phase_form_parses_and_validates(tmp_path):
    fixture = CA.parents[1] / "tests/marvel0/fixtures/chunk_accel_argv.json"
    if not fixture.is_file():
        pytest.skip("the page's argv fixture is not in this tree")
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("HARNESS_KEYS", "PHASE_E2E_FAULT", "PHASE_E2E_ENTRY_DIR")
    }
    env["CHUNK_ACCEL_OPENPI_ROOT"] = str(OPENPI)
    proc = subprocess.run(
        [str(OPENPI_PY), "-c", _PARSE_PAGE_ARGV, str(CA), str(fixture), str(tmp_path)],
        cwd=str(tmp_path), env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-2500:]
    line = next(row for row in proc.stdout.splitlines() if row.startswith("PAGE_ARGV "))
    found = json.loads(line.removeprefix("PAGE_ARGV "))
    for key, mode in (
        ("v3_plain", "plain"),
        ("v3_rtc", "rtc"),
        ("v3_vlash", "vlash"),
    ):
        got = found[key]
        assert got["mode"] == mode and got["rtc"] is (mode == "rtc"), (key, got)
        assert (
            got["hz"] == 10 and got["step_by_step"] is False and got["binarize"] is True
        )
        assert got["params"] == [got["lam"], got["alpha"], False], (key, got)
        # the entry sets the client's inference mode itself; the page must not (init=False)
        assert got["inference_mode"] == "plain", (key, got)
    assert found["v3_plain"]["open_loop_horizon"] == 6
    assert found["v3_rtc"]["rtc_execute_horizon"] == 6
    assert found["v3_vlash"]["overlap"] == 2


@needs_openpi
@pytest.mark.parametrize("mode", ("rtc", "plain"))
def test_a_frozen_arm_trips_the_leash_and_the_client_survives(tmp_path, mode):
    """The robot stops reporting new poses 8.5 cm from where the plan expects the arm: the last target is out of the
    leash, the engine holds it (or walks it back to the arm), the escape (3 holds -> reset, or the client's own 0.10 m
    refusal) re-queries. (The path where plain's engine drops its plan inside a placeholder and hook B is called again
    before the client's own query is covered deterministically by test_schedulers.PlainRecoveryTests: every plain chunk
    here starts at the frozen arm, so this fault cannot run a plan away from it.)"""
    steps = 60
    with Rig(tmp_path, mode, latency=0.083) as rig:
        rig.robot.stuck_offset = (0.0, 0.085, 0.0)
        rig.robot.set_fault("freeze", after_pose_calls=25)
        rc, out = run_client(
            client_args(rig, tmp_path, steps=steps, episodes=1),
            tmp_path,
            keys=manual_keys(steps, 1),
        )
        assert rc == 0 and "Traceback" not in out, out[-4000:]
    (m,) = demos(tmp_path).values()
    ca = m["eval"]["chunk_accel"]
    lines = assert_trace(ca, ticks_at_least=20)
    assert_envelope(lines, 2.5)  # never beyond the leash unless holding
    assert ca["leash_holds"] >= 1, ca
    # the escape: the third hold drops the plan, the client refuses a target beyond 0.10 m, or (a last target the arm
    # has been left behind by) the engine walks it back toward the arm. Plain also escapes through the loop's own next
    # query, whose chunk starts at the frozen arm: every run of holds ends with a new chunk within the open-loop
    # horizon (6). Without the placeholder's gate allowance (replay of the 11-02 fixes) that query comes after at most
    # 2 holds, before a third could drop the plan.
    escapes = ca["hold_resets"] + m["eval"]["rejected_jumps"] + ca["leash_recoveries"]
    if mode == "plain":
        holds = [r["k"] for r in lines if r["kind"] == "tick" and r["hold"]]
        chunks = [r["k"] for r in lines if r["kind"] == "chunk"]
        ends = [k for k in holds if k + 1 not in holds]  # the last hold of each run
        for k in ends:
            assert any(k < c <= k + 6 for c in chunks) or k == max(
                r["k"] for r in lines if r["kind"] == "tick"
            ), (k, holds, chunks)
        assert ends, holds  # plain's escape is the per-run check above
    else:
        assert escapes >= 1, (
            ca["hold_resets"],
            m["eval"]["rejected_jumps"],
            ca["leash_recoveries"],
        )


@needs_openpi
def test_a_non_finite_chunk_aborts_the_episode_and_nothing_non_finite_is_sent(tmp_path):
    steps = 40
    with Rig(
        tmp_path, "plain", latency=0.05, policy_args=["--nan-at-request", "3"]
    ) as rig:
        rc, out = run_client(
            client_args(rig, tmp_path, steps=steps, episodes=1),
            tmp_path,
            keys=manual_keys(steps, 1),
        )
        assert rc != 0 and "NonFiniteChunk" in out, out[-3000:]
    # manual mode closes the demo as aborted through recorder.close (it is kept as discarded_NNNN); its chunk_accel
    # says how it ended
    (m,) = demos(tmp_path, prefix="discarded_").values()
    assert m["eval"]["outcome"] == "aborted"
    ca = m["eval"]["chunk_accel"]
    assert ca["outcome"] == "exception:NonFiniteChunk"
    lines = assert_trace(ca, ticks_at_least=5)
    sent = [rec["out"] for rec in lines if rec["kind"] == "sent"]
    assert sent and all(np.isfinite(row).all() for row in sent)
    assert lines[-1]["outcome"] == "exception:NonFiniteChunk"


class Beacon:
    """Rewrites a small JSON file every interval, like the live service's status or the FR3 health monitor."""

    def __init__(self, path, make, interval):
        self.path, self.make, self.interval = Path(path), make, interval
        self._stop = threading.Event()
        self._write()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _write(self):
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.make()))
        os.replace(tmp, self.path)

    def _run(self):
        while not self._stop.wait(self.interval):
            self._write()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)


class LeviFiles:
    """The two files LEVI's unattended mode checks: the live service's status (C4) and the FR3 health monitor's (C3)."""

    def __init__(self, tmp):
        tmp = Path(tmp)
        roots = [str(tmp / "rollouts")]
        self.status, self.health = tmp / "levi_status.json", tmp / "fr3_health.json"

        def status():
            return {
                "schema": "levi.live.status.v1", "pid": os.getpid(), "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "state": "idle", "accepts_sessions": True, "ui_url": "http://127.0.0.1:7880", "core_port": 7881,
                "watch_roots": roots, "queue_depth": 0,
                "gpu": {"mode": "timeshare", "vllm_state": "asleep", "policy_server_seen": True}, "datasets": {},
                "last_error": None,
            }  # fmt: skip

        def health():
            now = time.time()
            return {
                "schema": "levi.fr3.health.v1", "updated_at": now, "updated_at_epoch": now, "robot_mode": 2,
                "robot_mode_name": "MOVE", "current_errors": [], "last_motion_errors": [], "hardware_active": True,
                "controller_active": True, "command_success_rate": 1.0, "red_light": False, "reasons": [],
                "monitor_pid": os.getpid(),
            }  # fmt: skip

        (tmp / "rollouts").mkdir(exist_ok=True)
        self.beacons = [
            Beacon(self.status, status, 1.0),
            Beacon(self.health, health, 0.25),
        ]

    def __enter__(self):
        return {"status": self.status, "health": self.health}

    def __exit__(self, *exc):
        for beacon in self.beacons:
            beacon.stop()


@needs_openpi
def test_an_engine_exception_aborts_the_episode_with_the_trace_flushed_and_its_sha_in_abort_detail(
    tmp_path,
):
    steps = 40
    with LeviFiles(tmp_path) as levi, Rig(tmp_path, "rtc", latency=0.083) as rig:
        args = client_args(rig, tmp_path, steps=steps, episodes=1, levi=levi)
        rc, out = run_client(args, tmp_path, fault={"engine_raise_at_commit": 25})
        assert rc != 0 and "injected engine failure" in out, out[-3000:]
    aborted = demos(tmp_path, prefix="incomplete_")
    assert len(aborted) == 1, list(aborted)
    (m,) = aborted.values()
    assert (
        m["eval"]["outcome"] == "aborted"
        and m["eval"]["abort_reason"] == "exception:RuntimeError"
    )
    ca = m["eval"]["abort_detail"]["chunk_accel"]
    assert ca["mode"] == "rtc" and ca["outcome"] == "exception:RuntimeError"
    lines = assert_trace(ca, ticks_at_least=15)
    assert lines[-1]["outcome"] == "exception:RuntimeError"
    assert ca["trace"]["sha256"] == sha256(ca["trace"]["file"])
    # the failing commit never produced a tick line, the ones before it are all there
    assert sum(1 for rec in lines if rec["kind"] == "tick") == 24


@needs_openpi
def test_unattended_levi_mode_records_the_same_fields(tmp_path):
    steps = 30
    with LeviFiles(tmp_path) as levi, Rig(tmp_path, "plain", latency=0.083) as rig:
        rc, out = run_client(
            client_args(rig, tmp_path, steps=steps, episodes=1, levi=levi), tmp_path
        )
        assert rc == 0, out[-4000:]
    (m,) = demos(tmp_path).values()
    assert (
        m["eval"]["unattended"] is True and m["eval"]["chunk_accel"]["mode"] == "plain"
    )
    assert_envelope(assert_trace(m["eval"]["chunk_accel"], ticks_at_least=20), 2.5)
    assert m["policy"]["chunk_accel"]["rule"] == "v3"


@needs_openpi
def test_an_installed_copy_runs_with_its_receipt_and_reports_no_code_drift(tmp_path):
    """install.py --apply into a copy of the client tree, then the RTC run FROM the installed files: the openpi root is
    found from the entry's own location, the receipt matches, code_drift is false. A client file edited afterwards
    turns it on and names the file."""
    import install

    root = tmp_path / "openpi"
    (root / "examples/fr3_local").mkdir(parents=True)
    for p in (OPENPI / "examples/fr3_local").glob("*.py"):
        shutil.copyfile(p, root / "examples/fr3_local" / p.name)
    (root / "lab/vlash").mkdir(parents=True)
    shutil.copyfile(OPENPI / "lab/vlash/runtime.py", root / "lab/vlash/runtime.py")
    install.install(root, apply=True)
    steps = 40
    run_dir = tmp_path / "run"
    with Rig(run_dir, "rtc", latency=0.083) as rig:
        rc, out = run_client(client_args(rig, run_dir, steps=steps, episodes=1), run_dir, keys=manual_keys(steps, 1),
                             entry_dir=root / "lab/chunk_accel")  # fmt: skip
        assert rc == 0, out[-4000:]
    (m,) = demos(run_dir).values()
    header = m["policy"]["chunk_accel"]
    assert header["code_drift"] is False, header["receipt"]
    assert header["receipt"]["found"] is True and header["receipt"]["drift"] == []
    assert header["root"] == str(root)
    assert header["shas"]["rc"] == sha256(
        root / "examples/fr3_local/run_robotiq_client.py"
    )
    # the same install, one client line changed afterwards: still runs (RTC), but says so
    rc_path = root / "examples/fr3_local/run_robotiq_client.py"
    rc_path.write_text(rc_path.read_text() + "\n# edited after the install\n")
    run2 = tmp_path / "run2"
    with Rig(run2, "rtc", latency=0.083) as rig:
        rc, out = run_client(client_args(rig, run2, steps=steps, episodes=1), run2, keys=manual_keys(steps, 1),
                             entry_dir=root / "lab/chunk_accel")  # fmt: skip
        assert rc == 0, out[-4000:]
        assert "changed since the install receipt" in out and "code_drift=True" in out
    (m2,) = demos(run2).values()
    assert m2["policy"]["chunk_accel"]["code_drift"] is True and m2["policy"][
        "chunk_accel"
    ]["receipt"]["drift"] == ["rc"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))
