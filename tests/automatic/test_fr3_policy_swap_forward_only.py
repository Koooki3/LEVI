"""Built-in policy service for a run with only the forward role (human_assisted
reset): one managed unit, started before any arm I/O, with its output captured
to a file that is tailed to the terminal on failure. Fake systemd, ``/proc``,
``nvidia-smi`` and arm; no unit tool, GPU or socket is touched."""

import os
import stat

import pytest
from fr3_swap_fakes import HOME, Proc, Smi, Time, Units, refuse_unit_tools
from test_fr3_policy_swap import DEPLOYMENTS, SAFETY, World, block, policies

from levi.automatic import fr3_policy_swap as S
from levi.automatic import state_machine as sm


@pytest.fixture(autouse=True)
def no_unit_tools(monkeypatch):
    refuse_unit_tools(monkeypatch)


def only_forward(value):
    value["services"] = {"forward": value["services"]["forward"]}
    return value


def fo_parse(tmp_path, **changes):
    return S.parse(
        only_forward(block(tmp_path, **changes)),
        policies={"forward": policies()["forward"]},
        deployments={"forward": DEPLOYMENTS["forward"]},
        safety=SAFETY,
    )


class FoWorld(World):
    """``World`` with the forward service only and a captured service log."""

    def __init__(self, tmp_path, **changes):
        self.tmp = tmp_path
        self.time = Time()
        self.proc = Proc(tmp_path / "proc")
        self.gpu = Smi()
        self.fence = sm.MotionFence()
        self.steps = 0
        self.pose = list(HOME)
        self.cancel = False
        self.handshakes = []
        self.identity = {}
        self.hook_errors = []
        self.warmups = []
        self.emitted = []
        self.units = Units(self.proc, self.gpu, hook=self.check_invariants)
        self.time.tick = self.units.tick
        self.cfg = fo_parse(tmp_path, **changes)
        self.log_dir = tmp_path / "rollouts" / ".aeri" / "policy-swap"
        self.log_path = S.log_path(tmp_path / "rollouts", "run-1")
        self.log = S.SwapLog.create(self.log_path, run_id="run-1", units={})
        self.switcher = self.make(self.log)

    def make(self, log):
        return S.PolicySwitcher(
            self.cfg,
            log=log,
            handshake=self.handshake,
            run_id="run-1",
            systemd=self.units,
            proc_root=self.proc.root,
            cgroup_root=self.proc.cgroup_root,
            gpu=S.Gpu(self.gpu),
            clock=self.time.clock,
            sleep=self.time.sleep,
            poll_s=0.01,
            service_log_dir=self.log_dir,
            emit=self.emitted.append,
        )

    def service_log(self):
        return self.log_dir / "run-1-forward.service.log"


# --- the binding block ------------------------------------------------------------------


def test_parse_accepts_the_forward_only_block(tmp_path):
    cfg = fo_parse(tmp_path)
    assert list(cfg.services) == ["forward"]
    summary = S.summary(cfg)
    assert summary["units"] == {"forward": "levi-aeri-policy-forward"}
    assert summary["ports"] == {"forward": 8000}
    assert summary["mem_fraction"] == {"forward": 0.22}
    assert summary["services_started_by_check"] is False
    argv = S.start_argv(cfg, cfg.services["forward"])
    assert "--setenv=XLA_PYTHON_CLIENT_MEM_FRACTION=0.22" in argv


def test_forward_only_block_with_the_reset_service_is_refused(tmp_path):
    with pytest.raises(ValueError, match="services requires exactly forward"):
        S.parse(
            block(tmp_path),
            policies={"forward": policies()["forward"]},
            deployments={"forward": DEPLOYMENTS["forward"]},
            safety=SAFETY,
        )


def test_two_role_policies_still_need_both_services(tmp_path):
    with pytest.raises(ValueError, match="services requires exactly forward and reset"):
        S.parse(
            only_forward(block(tmp_path)),
            policies=policies(),
            deployments=DEPLOYMENTS,
            safety=SAFETY,
        )


def test_a_reset_only_binding_is_refused(tmp_path):
    with pytest.raises(ValueError, match="forward"):
        S.parse(
            only_forward(block(tmp_path)),
            policies={"reset": policies()["reset"]},
            deployments={"reset": DEPLOYMENTS["reset"]},
            safety=SAFETY,
        )


@pytest.mark.parametrize(
    "field",
    ["unit", "argv", "working_directory", "mem_fraction"],
)
def test_forward_only_still_requires_every_service_field(tmp_path, field):
    value = only_forward(block(tmp_path))
    del value["services"]["forward"][field]
    with pytest.raises(ValueError, match="requires exactly"):
        S.parse(
            value,
            policies={"forward": policies()["forward"]},
            deployments={"forward": DEPLOYMENTS["forward"]},
            safety=SAFETY,
        )


def test_forward_only_refuses_a_missing_checkpoint_in_argv(tmp_path):
    value = only_forward(block(tmp_path))
    argv = value["services"]["forward"]["argv"]
    argv[argv.index("/models/forward")] = "/models/other"
    with pytest.raises(ValueError, match="frozen forward checkpoint_dir"):
        S.parse(
            value,
            policies={"forward": policies()["forward"]},
            deployments={"forward": DEPLOYMENTS["forward"]},
            safety=SAFETY,
        )


def test_forward_only_refuses_a_mem_fraction_over_the_budget(tmp_path):
    value = only_forward(block(tmp_path))
    value["services"]["forward"]["mem_fraction"] = 0.3
    with pytest.raises(ValueError, match="exceeds max_total_fraction"):
        S.parse(
            value,
            policies={"forward": policies()["forward"]},
            deployments={"forward": DEPLOYMENTS["forward"]},
            safety=SAFETY,
        )


# --- startup, warm-up, shutdown -----------------------------------------------------------


def test_startup_runs_one_forward_unit_and_warms_once_without_motion(tmp_path):
    w = FoWorld(tmp_path)
    client = w.switcher.startup("forward")
    assert client.role == "forward" and w.handshakes == ["forward"]
    assert w.warmups == []  # no arm exists yet
    assert [c[0] for c in w.units.calls].count("systemd-run") == 1
    w.bind()
    assert w.switcher.ensure("forward") is None
    assert w.warmups == ["forward"] and w.steps == 0
    assert w.switcher.ensure("forward") is None  # idempotent: no second warm-up
    assert w.warmups == ["forward"] and len(w.starts()) == 1
    assert w.records().count("warm") == 1
    assert w.hook_errors == []
    w.switcher.shutdown()
    assert w.units.units == {} and w.proc.sockets == {}
    assert [
        r["service_state"]
        for r in S.read_log(w.log_path)[0]
        if r["record"] == "committed"
    ] == ["forward", "none"]
    assert S.pending(tmp_path / "rollouts") == []


def test_a_role_the_binding_never_declared_is_refused_and_latches(tmp_path):
    w = FoWorld(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    before = len(w.units.calls)
    with pytest.raises(S.SwapError) as caught:
        w.switcher.ensure("reset")
    assert caught.value.code == "role_not_configured"
    assert w.switcher.latched and len(w.units.calls) == before
    with pytest.raises(S.SwapError, match="latched"):
        w.switcher.ensure("forward")


def test_startup_of_an_undeclared_role_is_refused_without_systemd(tmp_path):
    w = FoWorld(tmp_path)
    with pytest.raises(S.SwapError) as caught:
        w.switcher.startup("reset")
    assert caught.value.code == "role_not_configured" and w.units.calls == []


def test_a_leftover_forward_unit_is_refused_and_not_stopped(tmp_path):
    w = FoWorld(tmp_path)
    w.units.units["levi-aeri-policy-forward"] = w.proc.spawn(
        ["x"], "0::/app.slice/levi-aeri-policy-forward.service", 8000
    )
    with pytest.raises(S.SwapError) as caught:
        w.switcher.startup("forward")
    assert caught.value.code == "unit_active"
    assert not any(c[:3] == ["systemctl", "--user", "stop"] for c in w.units.calls)
    assert w.starts() == [] and w.switcher.latched


def test_a_foreign_listener_on_the_port_is_refused_before_start(tmp_path):
    w = FoWorld(tmp_path)
    w.proc.spawn(["foreign"], "0::/app.slice/other.service", 8000)
    with pytest.raises(S.SwapError) as caught:
        w.switcher.startup("forward")
    assert caught.value.code == "port_taken" and w.starts() == []
    assert w.proc.alive()  # the foreign process was not touched


def test_the_gpu_budget_is_checked_before_the_unit_starts(tmp_path):
    w = FoWorld(tmp_path)
    w.gpu.other_mib = 5000
    with pytest.raises(S.SwapError) as caught:
        w.switcher.startup("forward")
    assert caught.value.code == "gpu_budget" and w.starts() == []
    assert w.switcher.latched


def test_a_refused_handshake_stops_only_the_unit_it_started(tmp_path):
    w = FoWorld(tmp_path)
    w.identity["forward"] = {"checkpoint_dir": "/models/other"}
    with pytest.raises(S.SwapError) as caught:
        w.switcher.startup("forward")
    assert caught.value.code == "handshake_refused"
    assert w.units.units == {} and w.proc.sockets == {}
    aborted = [r for r in S.read_log(w.log_path)[0] if r["record"] == "aborted"]
    assert aborted[0]["service_state"] == "none"
    assert w.switcher.latched


def test_a_warm_up_that_counts_motion_stops_the_unit_and_latches(tmp_path):
    w = FoWorld(tmp_path)
    w.switcher.startup("forward")
    w.bind()

    def moving(role, client):
        w.steps += 1
        return [[0.0] * 7]

    w.warmup = moving
    w.bind()
    with pytest.raises(S.SwapError) as caught:
        w.switcher.ensure("forward")
    assert caught.value.code == "motion_during_switch"
    assert w.units.units == {} and w.switcher.latched


def test_shutdown_stops_nothing_it_did_not_start(tmp_path):
    w = FoWorld(tmp_path)
    w.units.units["levi-aeri-policy-forward"] = w.proc.spawn(
        ["x"], "0::/app.slice/levi-aeri-policy-forward.service", 8000
    )
    with pytest.raises(S.SwapError):
        w.switcher.startup("forward")
    w.switcher.shutdown()
    assert "levi-aeri-policy-forward" in w.units.units
    assert not any(c[:3] == ["systemctl", "--user", "stop"] for c in w.units.calls)


# --- the service log ------------------------------------------------------------------------


def test_the_unit_writes_to_a_private_log_file(tmp_path):
    w = FoWorld(tmp_path)
    w.switcher.startup("forward")
    argv = w.starts()[0]
    path = w.service_log()
    assert f"--property=StandardOutput=append:{path}" in argv
    assert f"--property=StandardError=append:{path}" in argv
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not os.path.islink(path)


def test_a_healthy_run_prints_nothing_from_the_service(tmp_path):
    w = FoWorld(tmp_path)
    w.units.banner = "INFO loaded checkpoint\n"
    w.switcher.startup("forward")
    w.bind()
    w.switcher.ensure("forward")
    w.units.emit("levi-aeri-policy-forward", "INFO step\n" * 500)
    w.switcher.shutdown()
    assert w.emitted == []


def test_a_failed_handshake_tails_the_service_log_to_the_terminal(tmp_path):
    w = FoWorld(tmp_path)
    w.units.banner = "".join(f"line {n}\n" for n in range(100))
    w.identity["forward"] = {"checkpoint_dir": "/models/other"}
    with pytest.raises(S.SwapError):
        w.switcher.startup("forward")
    assert len(w.emitted) == 1
    text = w.emitted[0]
    assert "levi-aeri-policy-forward" in text and str(w.service_log()) in text
    assert "line 99" in text and "line 60" in text and "line 59" not in text
    record = next(r for r in S.read_log(w.log_path)[0] if r["record"] == "aborted")
    assert record["service_log_tail"][-1] == "line 99"
    assert len(record["service_log_tail"]) == 40


def test_a_warm_up_failure_tails_the_log(tmp_path):
    w = FoWorld(tmp_path)
    w.units.banner = "boot\n"
    w.switcher.startup("forward")

    def broken(role, client):
        w.units.emit("levi-aeri-policy-forward", "Traceback: CUDA out of memory\n")
        raise RuntimeError("model call failed")

    w.warmup = broken
    w.bind()
    with pytest.raises(S.SwapError) as caught:
        w.switcher.ensure("forward")
    assert caught.value.code == "warmup_refused"
    assert "CUDA out of memory" in w.emitted[0]


def test_a_service_that_exits_before_it_listens_shows_why(tmp_path):
    w = FoWorld(tmp_path, start_retries=0)
    w.units.banner = "ImportError: no module named jax\n"
    w.units.exit_early = 1
    with pytest.raises(S.SwapError) as caught:
        w.switcher.startup("forward")
    assert caught.value.code == "exited_before_ready"
    assert len(w.emitted) == 1 and "ImportError: no module named jax" in w.emitted[0]
    assert w.units.units == {}


def test_a_lost_service_shows_the_log_and_latches(tmp_path):
    w = FoWorld(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    w.switcher.ensure("forward")
    w.units.emit("levi-aeri-policy-forward", "FATAL killed by oom\n")
    pid = w.units.units.pop("levi-aeri-policy-forward")
    w.proc.kill(pid)
    with pytest.raises(S.SwapError) as caught:
        w.switcher.ensure("forward")
    assert caught.value.code == "service_lost"
    assert "FATAL killed by oom" in w.emitted[0]


def test_a_retry_shows_only_the_failed_attempt(tmp_path):
    w = FoWorld(tmp_path, start_retries=1)
    w.units.banner = "attempt output\n"
    w.units.exit_early = 1  # the first start dies before listening
    w.switcher.startup("forward")
    retry = next(
        r
        for r in S.read_log(w.log_path)[0]
        if r["record"] == "step" and r["step"] == "start_retry"
    )
    assert retry["detail"] == "exited_before_ready"
    assert retry["service_log_tail"] == ["attempt output"]
    assert len(w.emitted) == 1  # the second attempt succeeded: nothing more
    w.units.emit("levi-aeri-policy-forward", "later\n")
    w.bind()
    w.switcher.ensure("forward")
    assert len(w.emitted) == 1


def test_the_tail_strips_terminal_escapes_and_bounds_line_length(tmp_path):
    path = tmp_path / "x.log"
    path.write_bytes(b"ok\n\x1b[31mred\x1b[0m\n" + b"a" * 5000 + b"\n\xff\xfe\n")
    lines = S.tail_lines(path, 0, 10)
    assert "\x1b" not in "".join(lines)
    assert all(len(line) <= 300 for line in lines)
    assert lines[0] == "ok" and lines[1] == "[31mred[0m"


def test_the_tail_reads_only_from_the_start_offset_and_the_last_lines(tmp_path):
    path = tmp_path / "x.log"
    path.write_text("old\nnew1\nnew2\nnew3\n")
    assert S.tail_lines(path, 4, 2) == ["new2", "new3"]
    assert S.tail_lines(tmp_path / "missing.log", 0, 5) == []


def test_a_log_directory_with_a_symlinked_file_is_refused(tmp_path):
    w = FoWorld(tmp_path)
    w.log_dir.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "elsewhere"
    target.write_text("")
    w.service_log().symlink_to(target)
    with pytest.raises(S.SwapError) as caught:
        w.switcher.startup("forward")
    assert caught.value.code == "service_log_unusable" and w.starts() == []


@pytest.mark.parametrize("name", ["a%b", "a b", "a$b"])
def test_a_log_directory_systemd_would_expand_is_refused(tmp_path, name):
    w = FoWorld(tmp_path)
    with pytest.raises(ValueError, match="service log directory"):
        S.PolicySwitcher(
            w.cfg,
            log=w.log,
            handshake=w.handshake,
            run_id="run-1",
            systemd=w.units,
            service_log_dir=tmp_path / name,
        )


def test_the_terminal_tail_goes_to_stderr_by_default(tmp_path, capsys):
    w = FoWorld(tmp_path)
    w.units.banner = "boom\n"
    w.identity["forward"] = {"checkpoint_dir": "/models/other"}
    switcher = S.PolicySwitcher(
        w.cfg,
        log=w.log,
        handshake=w.handshake,
        run_id="run-1",
        systemd=w.units,
        proc_root=w.proc.root,
        cgroup_root=w.proc.cgroup_root,
        gpu=S.Gpu(w.gpu),
        clock=w.time.clock,
        sleep=w.time.sleep,
        poll_s=0.01,
        service_log_dir=w.log_dir,
    )
    with pytest.raises(S.SwapError):
        switcher.startup("forward")
    assert "boom" in capsys.readouterr().err


def test_the_policy_fault_detail_carries_the_last_log_lines(tmp_path):
    w = FoWorld(tmp_path)
    w.units.banner = "x\n" * 5 + "RuntimeError: weights missing\n"
    w.identity["forward"] = {"checkpoint_dir": "/models/other"}
    with pytest.raises(S.SwapError):
        w.switcher.startup("forward")
    w.bind()
    policy = S.SwitchingPolicy(w.switcher, {}, {}, {}, None, None, None, None)
    out = policy.acquire("forward", policy_epoch=1)
    assert out["code"] == "policy_switch_fault" and out["fault"] is True
    assert "weights missing" in out["detail"]
