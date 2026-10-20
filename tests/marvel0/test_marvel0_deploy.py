"""``levi/marvel0/deploy.py``: deploying the policy server from the marvel0 page.

The machine is fake from top to bottom: a temporary openpi checkout and
checkpoints, a fake ``/proc`` (socket tables, lock table, cgroups, the sockets
of our unit's processes), a scripted ``systemctl``/``systemd-run``/
``nvidia-smi``, a GPU lock that records, and a clock moved by hand. No process
is started and no socket is opened (an autouse fixture makes either fail the
test), and nothing reads the real ``/proc`` or a process folder.

The file has three parts: the pure pieces (unit command, environment, log
reading, state rules, GPU arithmetic), then the supervisor's deploy, then its
stop, status and log.
"""

import ast
import builtins
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import HTTPException
from site_sample import make_checkpoint, make_openpi_tree, make_site

from levi import own_units
from levi.marvel0 import deploy, record
from levi.marvel0 import site as marvel0_site
from levi.workspace import Workspace

FRACTIONS = {0.22: 8198, 0.3: 10807, 0.45: 15698}  # on the 32607 MiB card
TOTAL_MIB = 32607
SFT = "pi05_fr3_all_step49999"
CFG = "recap_cfg_r2_best_step14300_jax"
NOW = 1_792_000_000.0
PROC_PATH = re.compile(r"^/(proc|sys/fs/cgroup)(/|$)")


@dataclass(frozen=True)
class Model:
    """What the deploy takes from a catalog entry."""

    id: str
    config: str
    cfg_capable: bool = False
    verified: bool = True


SFT_MODEL = Model(SFT, "pi05_fr3_all_state")
CFG_MODEL = Model(CFG, "pi05_fr3_all_state_cfg", cfg_capable=True, verified=False)


@pytest.fixture(autouse=True)
def forbid_real_machine(monkeypatch):
    """A test never starts a process, signals one, opens a socket or reads the
    real ``/proc`` or ``/sys/fs/cgroup`` (the fake machine has its own)."""

    def refuse(*args, **kwargs):
        raise AssertionError("a test started a process or opened a socket")

    for owner, name in (
        (subprocess, "run"),
        (subprocess, "Popen"),
        (subprocess, "check_output"),
        (subprocess, "check_call"),
        (subprocess, "call"),
        (os, "system"),
        (os, "execv"),
        (os, "execvp"),
        (os, "kill"),
        (os, "killpg"),
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
    ):
        monkeypatch.setattr(owner, name, refuse)

    def guard(real):
        def wrapper(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)) and PROC_PATH.match(
                os.fsdecode(path)
            ):
                raise AssertionError(f"the real machine was read: {os.fsdecode(path)}")
            return real(path, *args, **kwargs)

        return wrapper

    for owner, name in (
        (builtins, "open"),
        (io, "open"),
        (os, "open"),
        (os, "scandir"),
        (os, "listdir"),
        (os, "readlink"),
        (os, "stat"),
        (os, "lstat"),
    ):
        monkeypatch.setattr(owner, name, guard(getattr(owner, name)))


@pytest.fixture
def site(tmp_path):
    return make_site(tmp_path)


# ====================================================================== part 1
# The pure pieces.


# --------------------------------------------------------------- the commands


def test_the_script_depends_on_rtc():
    assert deploy.serve_script(True) == "scripts/serve_policy_rtc.py"
    assert deploy.serve_script(False) == "scripts/serve_policy.py"


@pytest.mark.parametrize(
    "rtc, model, scale, expected_head, expected_flags",
    [
        (
            True,
            SFT_MODEL,
            None,
            "scripts/serve_policy_rtc.py",
            ["--port", "8000", "policy:checkpoint"],
        ),
        (
            False,
            SFT_MODEL,
            None,
            "scripts/serve_policy.py",
            ["--port", "8000", "policy:checkpoint"],
        ),
        (
            True,
            CFG_MODEL,
            1.0,
            "scripts/serve_policy_rtc.py",
            ["--port", "8000", "--cfg-guidance-scale", "1.0", "policy:checkpoint"],
        ),
        (
            False,
            CFG_MODEL,
            1.25,
            "scripts/serve_policy.py",
            ["--port", "8000", "--cfg-guidance-scale", "1.25", "policy:checkpoint"],
        ),
    ],
)
def test_the_server_command_has_the_proven_form(
    site, rtc, model, scale, expected_head, expected_flags
):
    ckpt = f"{site.checkpoints_root}/{model.id}"
    argv = deploy.server_argv(site, model, rtc, scale, ckpt)
    assert argv == [
        site.python,
        expected_head,
        *expected_flags,
        "--policy.config",
        model.config,
        "--policy.dir",
        ckpt,
    ]
    assert "uv" not in argv and "run" not in argv, "the venv python runs directly"


def test_the_guidance_flag_goes_with_a_cfg_model_and_only_with_one(site):
    ckpt = f"{site.checkpoints_root}/{CFG}"
    with pytest.raises(ValueError):
        deploy.server_argv(site, SFT_MODEL, True, 1.0, ckpt)
    with pytest.raises(ValueError):
        deploy.server_argv(site, CFG_MODEL, True, None, ckpt)


@pytest.mark.parametrize("bad", ["/a b/ckpt", "/a$b/ckpt", "/a%b/ckpt", "/a\nb"])
def test_a_token_systemd_would_misread_is_refused(site, bad):
    with pytest.raises(ValueError):
        deploy.server_argv(site, SFT_MODEL, True, None, bad)


MANAGER_ENV = """\
HOME=/home/someone
PATH=/usr/local/bin:/usr/bin
AMENT_PREFIX_PATH=/opt/ros/jazzy
ROS_DISTRO=jazzy
ROS_DOMAIN_ID=7
RMW_IMPLEMENTATION=rmw_fastrtps_cpp
COLCON_PREFIX_PATH=/somewhere/install
CMAKE_PREFIX_PATH=/opt/ros/jazzy
CONDA_PREFIX=/home/someone/miniconda3
CONDA_DEFAULT_ENV=base
VIRTUAL_ENV=/home/someone/venv
PYTHONPATH=/opt/ros/jazzy/lib/python3/site-packages
PYTHONHOME=/x
LD_LIBRARY_PATH={ros}/lib:/usr/lib/x:{franka}/install/lib:{ros}bag/lib
"""


def manager_env(site):
    ros, franka = site.ros_prefixes
    return MANAGER_ENV.format(ros=ros, franka=franka)


def test_the_ros_and_conda_variables_are_taken_out_of_the_unit(site):
    env = deploy.unit_env(manager_env(site), site)
    assert sorted(env.unset) == [
        "AMENT_PREFIX_PATH",
        "CMAKE_PREFIX_PATH",
        "COLCON_PREFIX_PATH",
        "CONDA_DEFAULT_ENV",
        "CONDA_PREFIX",
        "PYTHONHOME",
        "PYTHONPATH",
        "RMW_IMPLEMENTATION",
        "ROS_DISTRO",
        "ROS_DOMAIN_ID",
        "VIRTUAL_ENV",
    ]
    ros = site.ros_prefixes[0]
    # Only the entries under a ROS prefix go; /opt/rosbag-like names stay.
    assert env.setenv["LD_LIBRARY_PATH"] == f"/usr/lib/x:{ros}bag/lib"
    assert env.setenv["PYTHONUNBUFFERED"] == "1"
    assert env.setenv["no_proxy"] == "localhost,127.0.0.1,::1"
    assert not set(env.unset) & set(env.setenv), "no name is set and unset"
    assert not [n for n in env.setenv if n.startswith(("ROS_", "AMENT_"))]


def test_a_library_path_that_is_all_ros_is_unset(site):
    ros, franka = site.ros_prefixes
    text = f"LD_LIBRARY_PATH={ros}/lib:{franka}/install/lib\nHOME=/h\n"
    env = deploy.unit_env(text, site)
    assert env.unset == ["LD_LIBRARY_PATH"]
    assert "LD_LIBRARY_PATH" not in env.setenv


def test_a_clean_environment_unsets_nothing(site):
    env = deploy.unit_env("HOME=/h\nPATH=/usr/bin\n", site)
    assert env.unset == []
    assert sorted(env.setenv) == ["PYTHONUNBUFFERED", "no_proxy"]


def test_library_entries_systemd_would_expand_are_dropped(site):
    text = "LD_LIBRARY_PATH=/ok/lib:/has space/lib:/has$dollar:/has%pct\n"
    assert deploy.unit_env(text, site).setenv["LD_LIBRARY_PATH"] == "/ok/lib"


def test_quoted_values_of_show_environment_are_read(site):
    ros = site.ros_prefixes[0]
    text = f"LD_LIBRARY_PATH='{ros}/lib:/usr/lib/y'\nFOO=$'a\\nb'\n"
    assert deploy.unit_env(text, site).setenv["LD_LIBRARY_PATH"] == "/usr/lib/y"


def systemd_run(site, model=SFT_MODEL, rtc=True, fraction=0.22, scale=None, **kw):
    unit = record.unit_name(model.id)
    ckpt = f"{site.checkpoints_root}/{model.id}"
    log = kw.pop("log", f"/state/logs/{unit}-20261019T000000Z.log")
    env = kw.pop("env", deploy.unit_env("HOME=/h\n", site))
    return (
        unit,
        ckpt,
        deploy.systemd_run_argv(
            site, unit, model, rtc, fraction, scale, log, env, ckpt
        ),
    )


def test_the_systemd_run_command_is_exactly_the_specified_unit(site):
    unit, ckpt, argv = systemd_run(site)
    log = f"/state/logs/{unit}-20261019T000000Z.log"
    assert argv == [
        "systemd-run",
        "--user",
        "--quiet",
        f"--unit={unit}",
        f"--description=LEVI marvel0 policy server {SFT}",
        "--collect",
        f"--working-directory={site.openpi_checkout}",
        "--property=KillMode=control-group",
        "--property=KillSignal=SIGINT",
        "--property=TimeoutStopSec=90",
        "--property=Restart=no",
        "--property=Nice=10",
        "--property=CPUAffinity=8-15",
        f"--property=StandardOutput=append:{log}",
        f"--property=StandardError=append:{log}",
        "--setenv=XLA_PYTHON_CLIENT_MEM_FRACTION=0.22",
        "--setenv=PYTHONUNBUFFERED=1",
        "--setenv=no_proxy=localhost,127.0.0.1,::1",
        "--",
        site.python,
        "scripts/serve_policy_rtc.py",
        "--port",
        "8000",
        "policy:checkpoint",
        "--policy.config",
        "pi05_fr3_all_state",
        "--policy.dir",
        ckpt,
    ]


def test_the_command_never_goes_through_a_shell_or_uv_or_a_restart(site):
    _, _, argv = systemd_run(site)
    text = " ".join(argv)
    for forbidden in ("bash", " sh ", "uv run", "Restart=on", "Delegate", "ExecStop"):
        assert forbidden not in text
    assert argv[0] == "systemd-run" and argv[1] == "--user"


def test_cfg_fraction_quota_and_unset_variables_are_in_the_command(tmp_path):
    site = make_site(tmp_path, unit={"cpu_quota_percent": 50, "nice": 5})
    env = deploy.unit_env(manager_env(site), site)
    _, _, argv = systemd_run(
        site, CFG_MODEL, rtc=False, fraction=0.45, scale=1.0, env=env
    )
    assert "--property=CPUQuota=50%" in argv
    assert "--property=Nice=5" in argv
    unset = next(a for a in argv if a.startswith("--property=UnsetEnvironment="))
    names = unset.split("=", 2)[2].split(" ")  # one element, one space between names
    assert "AMENT_PREFIX_PATH" in names and "ROS_DISTRO" in names
    assert all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n) for n in names)
    assert "--setenv=XLA_PYTHON_CLIENT_MEM_FRACTION=0.45" in argv
    assert next(a for a in argv if a.startswith("--setenv=LD_LIBRARY_PATH=")).endswith(
        "bag/lib"
    )
    tail = argv[argv.index("--") + 1 :]
    assert tail[1] == "scripts/serve_policy.py"
    assert tail[2:6] == ["--port", "8000", "--cfg-guidance-scale", "1.0"]


def test_no_quota_and_no_unset_property_when_there_is_nothing_to_set(site):
    _, _, argv = systemd_run(site)
    assert not any("CPUQuota" in a or "UnsetEnvironment" in a for a in argv)


def test_the_executed_command_is_what_follows_the_double_dash(site):
    _, ckpt, argv = systemd_run(site)
    executed = argv[argv.index("--") + 1 :]
    assert executed[0] == site.python and executed[-1] == ckpt
    assert not [t for t in executed if record.UNSAFE.search(t)]


EXEC_START = (
    "{ path=%(py)s ; argv[]=%(argv)s ; ignore_errors=no ; start_time=[n/a] ; "
    "stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
)


def show_text(unit, argv, state="active", sub="running", load="loaded"):
    start = EXEC_START % {"py": argv[0], "argv": " ".join(argv)}
    return (
        f"Id={unit}.service\nLoadState={load}\nActiveState={state}\n"
        f"SubState={sub}\nExecStart={start}\n"
    )


def test_show_output_is_parsed_into_keys():
    parsed = deploy.parse_show(
        "Id=x.service\nExecStart={ path=/p ; argv[]=/p a=b ; }\n"
    )
    assert parsed["Id"] == "x.service"
    assert parsed["ExecStart"].startswith("{ path=/p ; argv[]=/p a=b")
    assert deploy.parse_show("garbage\n\nA=\n") == {"A": ""}


def test_a_unit_is_ours_only_with_the_same_id_and_the_same_command(site):
    unit, _, argv = systemd_run(site)
    executed = argv[argv.index("--") + 1 :]
    rec = {"unit": unit, "exec_argv": executed}
    assert deploy.same_unit(show_text(unit, executed), rec)
    assert not deploy.same_unit(show_text(unit + "x", executed), rec), "Id differs"
    changed = [*executed[:-1], executed[-1] + "-other"]
    assert not deploy.same_unit(show_text(unit, changed), rec), "other --policy.dir"
    assert not deploy.same_unit(show_text(unit, [*executed, "--extra"]), rec)
    assert not deploy.same_unit(show_text(unit, executed[:-2]), rec)
    assert not deploy.same_unit(f"Id={unit}.service\nExecStart=\n", rec)
    assert not deploy.same_unit("", rec)
    assert not deploy.same_unit(show_text(unit, executed), {"unit": unit})


# --------------------------------------------------------------- the log

CK = "/ckpt/pi05_fr3_all_step49999"
NORM = f"INFO:root:Using norm stats shipped with the checkpoint: {CK}/norm_stats.json"
RTC_ON = "INFO:openpi:RTC enabled: max_guidance_weight=5.0, schedule=exp, plain policy"
RTC_CFG = (
    "INFO:openpi:RTC enabled: max_guidance_weight=5.0, schedule=exp, "
    "advantage-CFG policy"
)
WARM = "INFO:root:RTC warm-up: plain 812 ms, rtc 15400 ms (includes compilation)"
SKIPPED = "WARNING:root:RTC warm-up skipped (boom); the first RTC request will compile"
CFG_LINE = "INFO:root:Advantage-CFG policy, guidance scale w=1.0"
LISTEN = "INFO:websockets.server:server listening on 0.0.0.0:8000"
OOM = (
    "jaxlib.xla_extension.XlaRuntimeError: RESOURCE_EXHAUSTED: Out of memory "
    "while trying to allocate 4294967296 bytes."
)
# What XLA's allocator logs when it retries a big allocation and carries on (the
# same "W bfc_allocator ... ran out of memory trying to allocate" prefix opens the
# fatal case in the gpu probe log, which is then followed by the error above).
BFC_WARNING = (
    "2026-10-19 09:00:01.000001: W external/xla/xla/tsl/framework/bfc_allocator.cc:501]"
    " Allocator (GPU_0_bfc) ran out of memory trying to allocate 2.00GiB "
    "(rounded to 2147483648)requested by op "
)
BFC_NOTE = (
    "The caller indicates that this is not a failure, but this may mean that there "
    "could be performance gains if more memory were available."
)
ARGPARSE = [
    "usage: serve_policy_rtc.py [-h] [--port INT] policy:checkpoint",
    "serve_policy_rtc.py: error: unrecognized arguments: --bogus",
]
TRACEBACK = [
    "Traceback (most recent call last):",
    '  File "serve_policy.py", line 12, in <module>',
    "ValueError: boom",
]
IN_USE = "OSError: [Errno 98] Address already in use"


def write_log(tmp_path, *lines, name="server.log"):
    path = tmp_path / name
    flat = []
    for line in lines:
        flat.extend(line if isinstance(line, list) else [line])
    path.write_text("\n".join(flat) + "\n")
    return path


def scan(path, port=8000):
    return deploy.scan(path, CK, port)


def test_a_rtc_server_is_complete_after_the_four_documented_lines(tmp_path):
    result = scan(
        write_log(tmp_path, "INFO:root:Loading model...", NORM, RTC_ON, WARM, LISTEN)
    )
    assert (result.norm, result.rtc_enabled, result.warmup, result.listening) == (
        True,
        True,
        True,
        True,
    )
    assert result.failure is None and not result.warmup_skipped and not result.cfg
    assert deploy.complete(result, rtc=True, cfg_capable=False)


def test_a_plain_server_needs_only_the_norm_stats_and_the_port(tmp_path):
    result = scan(write_log(tmp_path, NORM, LISTEN))
    assert deploy.complete(result, rtc=False, cfg_capable=False)
    assert not deploy.complete(result, rtc=True, cfg_capable=False)


def test_a_cfg_server_needs_the_guidance_line_too(tmp_path):
    with_line = scan(write_log(tmp_path, NORM, CFG_LINE, RTC_CFG, WARM, LISTEN))
    assert with_line.cfg and deploy.complete(with_line, rtc=True, cfg_capable=True)
    without = scan(write_log(tmp_path, NORM, RTC_CFG, WARM, LISTEN, name="b.log"))
    assert not deploy.complete(without, rtc=True, cfg_capable=True)


def test_the_norm_stats_must_be_the_ones_of_this_checkpoint(tmp_path):
    other = NORM.replace("pi05_fr3_all_step49999", "another_checkpoint")
    result = scan(write_log(tmp_path, other, RTC_ON, WARM, LISTEN))
    assert not result.norm
    assert not deploy.complete(result, rtc=True, cfg_capable=False)


def test_a_skipped_warm_up_is_not_a_finished_one(tmp_path):
    result = scan(write_log(tmp_path, NORM, RTC_ON, SKIPPED, LISTEN))
    assert result.warmup_skipped and not result.warmup and result.rtc_enabled
    assert not deploy.complete(result, rtc=True, cfg_capable=False)


def test_listening_on_another_port_does_not_count(tmp_path):
    result = scan(write_log(tmp_path, NORM, LISTEN.replace("8000", "8001")), port=8000)
    assert not result.listening
    assert scan(write_log(tmp_path, LISTEN, name="b.log"), port=8000).listening


@pytest.mark.parametrize(
    "lines, kind",
    [
        ([NORM, OOM], "xla_oom"),
        (["jaxlib.xla_extension.XlaRuntimeError: Out of memory"], "xla_oom"),
        ([NORM, BFC_WARNING, BFC_NOTE, LISTEN], None),
        ([BFC_WARNING], None),
        (ARGPARSE, "argparse"),
        (["error: the following arguments are required: --policy.dir"], "argparse"),
        ([IN_USE], "address_in_use"),
        (["OSError: Address already in use"], "address_in_use"),
        (TRACEBACK, "traceback"),
        ([NORM, RTC_ON, WARM, LISTEN], None),
    ],
)
def test_failures_are_classified_from_the_log(tmp_path, lines, kind):
    assert scan(write_log(tmp_path, *lines)).failure == kind


def test_oom_wins_over_the_traceback_that_carries_it(tmp_path):
    result = scan(write_log(tmp_path, TRACEBACK[:2], OOM, TRACEBACK[2]))
    assert result.failure == "xla_oom"


def test_a_fatal_oom_after_the_allocator_warning_is_still_an_oom(tmp_path):
    # The probe log's shape: the warning, then the error that follows it.
    result = scan(write_log(tmp_path, NORM, BFC_WARNING, BFC_NOTE, OOM))
    assert result.failure == "xla_oom"


def test_an_empty_or_missing_log_says_nothing(tmp_path):
    empty = scan(write_log(tmp_path))
    assert not (empty.norm or empty.listening or empty.failure) and empty.tail == []
    missing = scan(tmp_path / "nothing.log")
    assert not (missing.norm or missing.listening or missing.failure)
    assert missing.tail == []


def test_a_log_that_is_a_link_or_a_fifo_is_not_read(tmp_path):
    real = write_log(tmp_path, NORM, LISTEN, name="real.log")
    link = tmp_path / "link.log"
    link.symlink_to(real)
    assert not scan(link).listening
    fifo = tmp_path / "fifo.log"
    os.mkfifo(fifo)
    assert not scan(fifo).listening  # and it returned instead of blocking


def test_the_startup_lines_are_found_after_five_megabytes_of_connections(tmp_path):
    path = tmp_path / "long.log"
    with path.open("w") as handle:
        handle.write(f"{NORM}\n{RTC_ON}\n{WARM}\n{LISTEN}\n")
        line = "INFO:websockets.server:connection open\n"
        handle.write(line * (5 * 1024 * 1024 // len(line)))
    result = scan(path)
    assert deploy.complete(result, rtc=True, cfg_capable=False)
    assert result.tail and all("connection open" in t for t in result.tail)


def test_a_failure_at_the_end_of_a_long_log_is_found(tmp_path):
    path = tmp_path / "long.log"
    with path.open("w") as handle:
        handle.write(f"{NORM}\n{LISTEN}\n")
        line = "INFO:websockets.server:connection open\n"
        handle.write(line * (1024 * 1024 // len(line)))
        handle.write(OOM + "\n")
    assert scan(path).failure == "xla_oom"


def test_page_lines_are_printable_short_and_without_paths(tmp_path):
    long = "x" * 500
    path = write_log(
        tmp_path,
        "\x1b[31mred\x1b[0m text\x07 and\x00 nul",
        f"INFO:root:Using norm stats shipped with the checkpoint: {CK}/norm_stats.json",
        long,
        "",
        "http://127.0.0.1:5000/ is fine",
    )
    lines = deploy.page_lines(path, 10)
    assert lines[0] == "red text and nul"
    assert lines[1].endswith("checkpoint: norm_stats.json")
    assert len(lines[2]) == 300
    assert lines[3] == "http://127.0.0.1:5000/ is fine"
    assert len(lines) == 4, "empty lines are dropped"
    assert deploy.page_lines(path, 2) == lines[-2:]
    assert deploy.page_lines(tmp_path / "missing.log", 5) == []


def test_the_page_count_is_clamped(tmp_path):
    path = write_log(tmp_path, *[f"line {n}" for n in range(300)])
    assert len(deploy.page_lines(path, 1000)) == deploy.MAX_PAGE_LINES == 200
    assert deploy.page_lines(path, 0) == ["line 299"]
    assert deploy.page_lines(path, -4) == ["line 299"]


# --------------------------------------------------------------- the GPU


@pytest.mark.parametrize("fraction, needed", sorted(FRACTIONS.items()))
def test_the_vram_a_fraction_needs_on_the_32607_mib_card(fraction, needed):
    assert deploy.required_mib(fraction, TOTAL_MIB, 1024) == needed


def smi(text, code=0):
    return lambda argv, timeout: (code, text)


def test_one_gpu_line_is_read_as_used_and_total():
    seen = []

    def run(argv, timeout):
        seen.append(argv)
        return 0, "9100, 32607\n"

    gpu = deploy.read_gpu(run)
    assert (gpu.total_mib, gpu.used_mib, gpu.free_mib) == (32607, 9100, 23507)
    assert seen == [
        [
            "nvidia-smi",
            "--query-gpu=memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ]
    ]


@pytest.mark.parametrize(
    "text, code",
    [
        ("", 0),
        ("garbage\n", 0),
        ("9100, 32607\n10, 20000\n", 0),
        ("9100\n", 0),
        ("a, b\n", 0),
        ("-5, 100\n", 0),
        ("200, 100\n", 0),
        ("9100, 32607\n", 9),
        ("0, 0\n", 0),
    ],
)
def test_anything_but_one_sound_gpu_line_is_unreadable(text, code):
    with pytest.raises(deploy.Refuse) as caught:
        deploy.read_gpu(smi(text, code))
    assert caught.value.code == "gpu_unreadable" and caught.value.status == 503


# --------------------------------------------------------------- the runner


@pytest.mark.parametrize(
    "argv",
    [
        ["kill", "-9", "1"],
        ["pkill", "-f", "serve"],
        ["ss", "-ltnp"],
        ["bash", "-c", "true"],
        ["systemctl", "stop", "x.service"],  # no --user
        ["systemd-run", "--scope", "x"],
        [],
    ],
)
def test_the_runner_allows_only_user_systemd_and_nvidia_smi(argv):
    calls = []
    guarded = deploy.guarded(lambda a, t: calls.append(a) or (0, ""))
    with pytest.raises(ValueError):
        guarded(argv, 5)
    assert calls == []


def test_the_runner_lets_the_three_commands_through():
    calls = []
    guarded = deploy.guarded(lambda a, t: calls.append((a, t)) or (0, "ok"))
    assert guarded(["systemctl", "--user", "show", "x"], 5) == (0, "ok")
    assert guarded(["systemd-run", "--user", "--quiet", "--", "x"], 15) == (0, "ok")
    assert guarded(["nvidia-smi", "--query-gpu=memory.used"], 10) == (0, "ok")
    assert [t for _, t in calls] == [5, 15, 10]


def test_the_real_runner_returns_stdout_and_adds_stderr_on_failure(monkeypatch):
    class Done:
        def __init__(self, code, out, err):
            self.returncode, self.stdout, self.stderr = code, out, err

    answers = iter([Done(0, "out\n", "noise\n"), Done(1, "o\n", "Unknown property\n")])
    seen = []

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs))
        return next(answers)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert deploy.run_command(["systemctl", "--user", "x"], 5) == (0, "out\n")
    assert deploy.run_command(["systemd-run", "--user", "x"], 15) == (
        1,
        "o\nUnknown property\n",
    )
    assert seen[0][1]["timeout"] == 5 and not seen[0][1].get("shell")
    assert seen[0][1]["capture_output"] and seen[0][1]["text"]


def test_the_real_runner_answers_124_on_a_timeout_and_125_when_it_cannot_run(
    monkeypatch,
):
    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 5)

    def missing(argv, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(subprocess, "run", slow)
    code, text = deploy.run_command(["systemctl", "--user", "x"], 5)
    assert code == deploy.TIMED_OUT == 124 and text
    monkeypatch.setattr(subprocess, "run", missing)
    code, text = deploy.run_command(["systemctl", "--user", "x"], 5)
    assert code == deploy.COULD_NOT_RUN == 125 and "no such file" in text


def test_the_production_host_uses_the_guarded_runner_and_the_real_paths():
    host = deploy.production_host()
    with pytest.raises(ValueError):
        host.run(["kill", "-9", "1"], 5)
    assert host.proc_net is None and host.proc_root is None
    assert host.locks_path == "/proc/locks"
    assert host.background is True


def test_the_unsafe_class_is_the_one_the_other_swap_code_uses():
    source = (
        Path(deploy.__file__).resolve().parents[1] / "automatic" / "fr3_policy_swap.py"
    )
    match = re.search(
        r"^UNSAFE_PATH = re\.compile\((r\".*?\")\)$", source.read_text(), re.MULTILINE
    )
    assert match, "fr3_policy_swap.UNSAFE_PATH moved"
    assert ast.literal_eval(match.group(1)) == record.UNSAFE.pattern


def test_the_policy_unit_pattern_is_the_one_in_own_units():
    assert record.unit_name(SFT).startswith("levi-aeri-policy-")
    assert own_units.POLICY_UNIT.fullmatch(record.unit_name(SFT))


# --------------------------------------------------------------- the state rules

UNIT = record.unit_name(SFT)


def a_record(**changes):
    value = {
        "phase": "started",
        "unit": UNIT,
        "model_id": SFT,
        "rtc": True,
        "cfg_guidance_scale": None,
    }
    value.update(changes)
    return value


def a_scan(**changes):
    return deploy.Scan(**changes)


READY_SCAN = {"norm": True, "rtc_enabled": True, "warmup": True, "listening": True}


def facts(**changes):
    base = {
        "record": a_record(),
        "record_error": False,
        "unit": "active",
        "other_units": [],
        "net_ok": True,
        "listeners": [],
        "established": 0,
        "scan": None,
        "age_s": 20.0,
        "start_timeout_s": 600.0,
        "listening_age_s": None,
    }
    base.update(changes)
    return deploy.Facts(**base)


@pytest.mark.parametrize(
    "name, changes, state, reason, failure, stoppable",
    [
        (
            "nothing at all",
            {"record": None, "unit": "absent"},
            "idle",
            None,
            None,
            False,
        ),
        (
            "a server started by hand",
            {"record": None, "unit": "absent", "listeners": [False]},
            "foreign_listener",
            "hand_started",
            None,
            False,
        ),
        (
            "a hand-started server next to a stopped record",
            {
                "record": a_record(phase="stopped"),
                "unit": "absent",
                "listeners": [False, False],
            },
            "foreign_listener",
            "hand_started",
            None,
            False,
        ),
        (
            "a unit of another kind exists",
            {
                "record": None,
                "unit": "absent",
                "other_units": ["levi-aeri-policy-x.service"],
            },
            "idle",
            None,
            None,
            False,
        ),
        (
            "systemd-run is in flight",
            {"record": a_record(phase="intent"), "unit": "absent", "age_s": 10.0},
            "starting",
            None,
            None,
            False,
        ),
        (
            "the core died between record and systemd-run",
            {"record": a_record(phase="intent"), "unit": "absent", "age_s": 40.0},
            "failed",
            None,
            "intent_only",
            False,
        ),
        (
            "loading",
            {"unit": "activating"},
            "starting",
            None,
            None,
            True,
        ),
        (
            "listening before the log is complete",
            {"listeners": [True], "scan": a_scan(norm=True)},
            "starting",
            None,
            None,
            True,
        ),
        (
            "everything logged",
            {"listeners": [True], "scan": a_scan(**READY_SCAN)},
            "ready",
            None,
            None,
            True,
        ),
        (
            "everything logged but the socket is not in the table yet",
            {"listeners": [], "scan": a_scan(**READY_SCAN)},
            "starting",
            None,
            None,
            True,
        ),
        (
            "a plain server",
            {
                "record": a_record(rtc=False),
                "listeners": [True],
                "scan": a_scan(norm=True, listening=True),
            },
            "ready",
            None,
            None,
            True,
        ),
        (
            "rtc lines missing, just after listening",
            {
                "listeners": [True],
                "scan": a_scan(norm=True, listening=True),
                "listening_age_s": 1.0,
            },
            "starting",
            None,
            None,
            True,
        ),
        (
            "rtc lines missing, listening for a while",
            {
                "listeners": [True],
                "scan": a_scan(norm=True, listening=True),
                "listening_age_s": 6.0,
            },
            "failed",
            None,
            "rtc_not_ready",
            True,
        ),
        (
            "rtc warm-up skipped",
            {
                "listeners": [True],
                "scan": a_scan(
                    norm=True,
                    rtc_enabled=True,
                    warmup_skipped=True,
                    listening=True,
                ),
                "listening_age_s": 6.0,
            },
            "failed",
            None,
            "rtc_not_ready",
            True,
        ),
        (
            "the guidance line missing",
            {
                "record": a_record(rtc=False, cfg_guidance_scale=1.0),
                "listeners": [True],
                "scan": a_scan(norm=True, listening=True),
                "listening_age_s": 6.0,
            },
            "failed",
            None,
            "cfg_not_confirmed",
            True,
        ),
        (
            "out of memory, the unit still there",
            {"scan": a_scan(failure="xla_oom"), "unit": "active"},
            "failed",
            None,
            "xla_oom",
            True,
        ),
        (
            "out of memory, the unit gone",
            {"scan": a_scan(failure="xla_oom"), "unit": "absent"},
            "failed",
            None,
            "xla_oom",
            False,
        ),
        (
            "a bad command line",
            {"scan": a_scan(failure="argparse"), "unit": "absent"},
            "failed",
            None,
            "argparse",
            False,
        ),
        (
            "the port was taken",
            {"scan": a_scan(failure="address_in_use"), "unit": "absent"},
            "failed",
            None,
            "address_in_use",
            False,
        ),
        (
            "a traceback while starting",
            {"scan": a_scan(failure="traceback"), "unit": "absent"},
            "failed",
            None,
            "traceback",
            False,
        ),
        (
            "a traceback of one request on a server that is ready",
            {"listeners": [True], "scan": a_scan(failure="traceback", **READY_SCAN)},
            "ready",
            None,
            None,
            True,
        ),
        (
            "an out-of-memory error of one request on a server that is ready",
            {"listeners": [True], "scan": a_scan(failure="xla_oom", **READY_SCAN)},
            "ready",
            None,
            None,
            True,
        ),
        (
            "an out-of-memory error, the server had been ready and is gone",
            {"unit": "absent", "scan": a_scan(failure="xla_oom", **READY_SCAN)},
            "dead",
            "exited_after_ready",
            None,
            False,
        ),
        (
            "exited before it listened",
            {"unit": "absent", "scan": a_scan(norm=True)},
            "failed",
            None,
            "exited",
            False,
        ),
        (
            "exited after it listened",
            {"unit": "absent", "scan": a_scan(**READY_SCAN)},
            "dead",
            "exited_after_ready",
            None,
            False,
        ),
        (
            "stopped from outside the page",
            {"unit": "inactive", "scan": a_scan(**READY_SCAN)},
            "dead",
            "exited_after_ready",
            None,
            False,
        ),
        (
            "never complete within the start timeout",
            {"unit": "active", "age_s": 601.0, "scan": a_scan(norm=True)},
            "failed",
            None,
            "start_timeout",
            True,
        ),
        (
            "systemd-run failed",
            {"record": a_record(phase="start_failed"), "unit": "absent"},
            "failed",
            None,
            "start_failed",
            False,
        ),
        (
            "aborted before systemd-run",
            {"record": a_record(phase="aborted"), "unit": "absent"},
            "idle",
            None,
            None,
            False,
        ),
        (
            "stopped",
            {"record": a_record(phase="stopped"), "unit": "absent"},
            "idle",
            None,
            None,
            False,
        ),
        (
            "our unit is running but another socket owns the port",
            {"listeners": [False], "unit": "active"},
            "foreign_listener",
            "listener_not_ours",
            None,
            False,
        ),
        (
            "our unit's processes could not be read",
            {"listeners": [None], "unit": "active"},
            "foreign_listener",
            "owner_unknown",
            None,
            False,
        ),
        (
            "ours and another listener",
            {"listeners": [True, False], "unit": "active"},
            "foreign_listener",
            "listener_not_ours",
            None,
            False,
        ),
        (
            "stop requested, still shutting down",
            {
                "record": a_record(phase="stopping"),
                "unit": "deactivating",
                "listeners": [True],
            },
            "stopping",
            None,
            None,
            False,
        ),
        (
            "stop requested, unit still active and no stop going on (stale)",
            {"record": a_record(phase="stopping"), "unit": "active"},
            "stopping",
            None,
            None,
            True,
        ),
        (
            "stop requested, unit still activating and no stop going on",
            {"record": a_record(phase="stopping"), "unit": "activating"},
            "stopping",
            None,
            None,
            True,
        ),
        (
            "stop requested, unit active, the stop is going on right now",
            {
                "record": a_record(phase="stopping"),
                "unit": "active",
                "stop_in_flight": True,
            },
            "stopping",
            None,
            None,
            False,
        ),
        (
            "stop requested and done",
            {"record": a_record(phase="stopping"), "unit": "absent"},
            "idle",
            None,
            None,
            False,
        ),
        (
            "stopped by someone else while shutting down",
            {"unit": "deactivating", "listeners": [True]},
            "stopping",
            "stopped_elsewhere",
            None,
            False,
        ),
        (
            "socket tables unreadable",
            {"net_ok": False},
            "unknown",
            "net_unreadable",
            None,
            False,
        ),
        (
            "systemd unreadable with a record",
            {"unit": "unknown"},
            "unknown",
            "systemd_unreadable",
            None,
            False,
        ),
        (
            "unit list unreadable with a record",
            {"other_units": None},
            "unknown",
            "systemd_unreadable",
            None,
            False,
        ),
        (
            "unit list unreadable and a listener",
            {
                "record": None,
                "unit": "absent",
                "other_units": None,
                "listeners": [False],
            },
            "unknown",
            "systemd_unreadable",
            None,
            False,
        ),
        (
            "unit list unreadable, nothing else known",
            {"record": None, "unit": "absent", "other_units": None},
            "idle",
            None,
            None,
            False,
        ),
        (
            "record unreadable",
            {"record": None, "record_error": True},
            "unknown",
            "record_unreadable",
            None,
            False,
        ),
        (
            "stopped record but a unit of ours is running",
            {"record": a_record(phase="stopped"), "unit": "active"},
            "unknown",
            "unclassified",
            None,
            False,
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_the_state_follows_the_facts(name, changes, state, reason, failure, stoppable):
    result = deploy.derive(facts(**changes))
    assert (result.state, result.reason, result.failure, result.stoppable) == (
        state,
        reason,
        failure,
        stoppable,
    ), name
    assert result.state in deploy.STATES


def test_the_same_facts_always_give_the_same_state():
    one = deploy.derive(facts(listeners=[True], scan=a_scan(**READY_SCAN)))
    two = deploy.derive(facts(listeners=[True], scan=a_scan(**READY_SCAN)))
    assert one == two


def test_the_unreadable_sources_are_checked_before_everything_else():
    # A log full of success does not make a state out of an unreadable table.
    result = deploy.derive(
        facts(net_ok=False, listeners=[True], scan=a_scan(**READY_SCAN))
    )
    assert result.state == "unknown"


# ====================================================================== part 2
# The fake machine and the supervisor's deploy.

T0 = 1000.0
HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
    "   uid  timeout inode\n"
)
LOOPBACK = "0100007F"


def le4(text):
    """An IPv4 address as ``/proc/net/tcp`` writes it (little-endian word)."""
    return "".join(f"{int(part):02X}" for part in reversed(text.split(".")))


CGROUP = "/user.slice/user-1000.slice/user@1000.service/app.slice"
LIVE_UNIT = "levi-live"
REQ = "req-000001-aaaa"


class Clock:
    """Monotonic and wall time moved by hand; ``sleep`` only moves them."""

    def __init__(self):
        self.t = T0
        self.sleeps = []
        self.on_sleep = None

    def monotonic(self):
        return self.t

    def now(self):
        return NOW + (self.t - T0)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds
        if self.on_sleep:
            self.on_sleep(seconds)

    def advance(self, seconds):
        self.t += seconds


class FakeLock:
    """The GPU lock: every acquire and release is an event."""

    def __init__(self, marvel0, path):
        self.marvel0 = marvel0
        self.path = path
        self.held = False

    def acquire(self):
        self.marvel0.events.append(("lock", "acquire"))
        if self.marvel0.on_acquire:
            self.marvel0.on_acquire()
        if self.marvel0.lock_busy:
            return False
        self.held = True
        return True

    def release(self):
        if self.held:
            self.held = False
            self.marvel0.events.append(("lock", "release"))


class FakeCatalog:
    def __init__(self, *models):
        self.models = {m.id: m for m in models}

    def get(self, site, model_id):
        return self.models.get(model_id)


class Marvel0:
    """The fake machine behind one ``Supervisor``."""

    def __init__(self, tmp_path, monkeypatch, **site_override):
        self.tmp = Path(tmp_path)
        # The arm lock is the AERI runner's fixed file: here one of the test's.
        monkeypatch.setattr(marvel0_site, "ARM_LOCK_FILE", str(self.tmp / "arm.lock"))
        self.site = make_site(tmp_path, **site_override)
        make_openpi_tree(tmp_path)
        root = self.site.checkpoints_root
        make_checkpoint(root, SFT, config="pi05_fr3_all_state")
        make_checkpoint(root, CFG, config="pi05_fr3_all_state_cfg")
        self.proc = self.tmp / "proc"
        (self.proc / "net").mkdir(parents=True)
        self.cgroup = self.tmp / "cgroup"
        self.locks = self.proc / "locks"
        self.locks.write_text("")
        self.workspace = Workspace.at(self.tmp / "ws")
        self.clock = Clock()
        self.events = []
        self.calls = []
        self.units = {}  # name without .service -> {"active", "sub", "argv"}
        self.cgroups = {}  # other units with a cgroup: name -> pids
        self.foreign_units = []  # names list-units shows besides ours
        self.sockets = []
        self.gpu = [9100, TOTAL_MIB]
        self.gpu_text = None
        self.env_text = "HOME=/h\nPATH=/usr/bin\n"
        self.fail = set()
        self.run_raises = None
        self.stop_mode = "ok"
        self.aeri = []
        self.aeri_error = False
        self.robot = {"marker": "absent", "open_session": False}
        self.lock_busy = False
        self.locks_made = []
        self.on_systemd_run = None
        self.on_acquire = None
        self.on_stop = None
        self.systemd_run_answer = None
        self.held_at_run = None
        self.record_at_run = None
        self.host = deploy.Host(
            run=self.run,
            proc_net=self.proc / "net",
            proc_root=self.proc,
            cgroup_root=str(self.cgroup),
            locks_path=str(self.locks),
            gpu_lock=self.make_lock,
            aeri_runs=self.aeri_runs,
            robot_context=lambda: dict(self.robot),
            monotonic=self.clock.monotonic,
            now=self.clock.now,
            sleep=self.clock.sleep,
            background=False,
        )
        self.catalog = FakeCatalog(SFT_MODEL, CFG_MODEL)
        self.sup = deploy.Supervisor(
            self.host,
            site_loader=lambda: self.site,
            catalog=self.catalog,
            workspace=self.workspace,
        )
        real_write = record.write_record

        def spy(directory, value):
            self.events.append(("record", value.get("phase")))
            return real_write(directory, value)

        monkeypatch.setattr(record, "write_record", spy)
        self.write_net()

    # ---- the files of the deploy

    @property
    def state(self):
        return record.state_dir((), self.workspace, create=False)

    def record(self):
        return record.read_record(self.state)

    def systemd_runs(self):
        return [a for a, _ in self.calls if a[0] == "systemd-run"]

    def commands(self):
        return [tuple(a) for a, _ in self.calls]

    def stops(self):
        return [a for a, _ in self.calls if a[2:3] == ["stop"]]

    # ---- the kernel

    def write_net(self):
        rows = {"tcp": [], "tcp6": []}
        for n, s in enumerate(self.sockets):
            local = f"{s.get('local_addr', LOOPBACK)}:{s['port']:04X}"
            remote = f"{s.get('remote_addr', LOOPBACK)}:{s.get('remote', 0):04X}"
            rows[s.get("proto", "tcp")].append(
                f"   {n}: {local} {remote} {s['state']} 00000000:00000000 "
                f"00:00000000 00000000  1000        0 {s['inode']} 1 0 100 0 0 10 0\n"
            )
        for name, lines in rows.items():
            if f"net-{name}" not in self.fail:
                (self.proc / "net" / name).write_text(HEADER + "".join(lines))
            else:
                (self.proc / "net" / name).unlink(missing_ok=True)

    def listen(self, inode=111, port=8000, proto="tcp"):
        self.sockets.append(
            {"port": port, "state": "0A", "inode": inode, "proto": proto, "remote": 0}
        )
        self.write_net()

    def connect(self, client=50000, port=8000, base=900):
        self.sockets.append(
            {"port": port, "remote": client, "state": "01", "inode": base}
        )
        self.sockets.append(
            {"port": client, "remote": port, "state": "01", "inode": base + 1}
        )
        self.write_net()

    def accept_from(self, peer, client=51000, port=8000, base=910):
        """A client on another machine: only the accepted socket is ours."""
        self.sockets.append(
            {
                "port": port,
                "remote": client,
                "state": "01",
                "inode": base,
                "local_addr": le4("192.168.1.5"),
                "remote_addr": le4(peer),
            }
        )
        self.write_net()

    def connect_out(self, remote_host, remote_port=8000, local=50100, base=920):
        """This machine's own outgoing connection to a service elsewhere."""
        self.sockets.append(
            {
                "port": local,
                "remote": remote_port,
                "state": "01",
                "inode": base,
                "local_addr": le4("192.168.1.5"),
                "remote_addr": le4(remote_host),
            }
        )
        self.write_net()

    def disconnect(self):
        self.sockets = [s for s in self.sockets if s["state"] != "01"]
        self.write_net()

    def hold(self, path, pid):
        """A ``/proc/locks`` row: ``pid`` holds the flock on ``path``."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        info = os.stat(path)
        with self.locks.open("a") as handle:
            handle.write(
                f"1: FLOCK  ADVISORY  WRITE {pid} "
                f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino} 0 EOF\n"
            )

    def own(self, unit, pid, inodes=()):
        """``pid`` is in ``unit``'s cgroup and holds the sockets ``inodes``."""
        group = self.cgroup / CGROUP.lstrip("/") / f"{unit}.service"
        group.mkdir(parents=True, exist_ok=True)
        with (group / "cgroup.procs").open("a") as handle:
            handle.write(f"{pid}\n")
        if not unit.startswith("levi-aeri-policy-"):
            self.cgroups.setdefault(unit, []).append(pid)
        fds = self.proc / str(pid) / "fd"
        fds.mkdir(parents=True, exist_ok=True)
        for n, inode in enumerate(inodes, start=3):
            link = fds / str(n)
            if not link.is_symlink():
                link.symlink_to(f"socket:[{inode}]")

    # ---- systemd, nvidia-smi

    def run(self, argv, timeout_s):
        argv = list(argv)
        assert argv and argv[0] in deploy.ALLOWED_COMMANDS, argv
        if argv[0] != "nvidia-smi":
            assert "--user" in argv[1:3], argv
        for bad in ("kill", "pkill", "pgrep", "ss", "lsof", "fuser"):
            assert bad not in argv, argv
        self.calls.append((argv, timeout_s))
        self.events.append(("run", tuple(argv)))
        if argv[0] == "nvidia-smi":
            if "nvidia-smi" in self.fail:
                return 9, ""
            if self.gpu_text is not None:
                return 0, self.gpu_text
            return 0, f"{self.gpu[0]}, {self.gpu[1]}\n"
        if argv[0] == "systemd-run":
            return self.systemd_run(argv)
        verb = argv[2]
        if verb == "list-units":
            if "list-units" in self.fail:
                return 1, ""
            lines = []
            for name in [*self.units, *self.foreign_units]:
                if not name.startswith("levi-aeri-policy-"):
                    continue
                active = self.units.get(name, {}).get("active", "active")
                full = name if "." in name else f"{name}.service"
                lines.append(f"{full} loaded {active} running LEVI policy\n")
            return 0, "".join(lines)
        if verb == "show-environment":
            return (1, "") if "show-environment" in self.fail else (0, self.env_text)
        if verb == "show":
            return self.show(argv)
        if verb == "stop":
            return self.stop(argv[3].removesuffix(".service"))
        raise AssertionError(f"unscripted command {argv}")

    def show(self, argv):
        unit = argv[-1].removesuffix(".service")
        props = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
        if "ControlGroup" in props:
            if "show-cgroup" in self.fail:
                return 1, ""
            known = unit in self.units or unit in self.cgroups
            return 0, f"{CGROUP}/{unit}.service\n" if known else "\n"
        if "show-state" in self.fail:
            return 1, ""
        item = self.units.get(unit)
        values = {
            "Id": f"{unit}.service",
            "LoadState": "loaded" if item else "not-found",
            "ActiveState": item["active"] if item else "inactive",
            "SubState": item["sub"] if item else "dead",
            "ExecStart": (
                EXEC_START % {"py": item["argv"][0], "argv": " ".join(item["argv"])}
                if item
                else ""
            ),
        }
        return 0, "".join(f"{p}={values[p]}\n" for p in props)

    def systemd_run(self, argv):
        if self.on_systemd_run:
            self.on_systemd_run(argv)
        if self.run_raises:
            raise self.run_raises
        if self.systemd_run_answer:
            return self.systemd_run_answer
        if "systemd-run" in self.fail:
            return 1, "Failed to set unit properties: Unknown assignment\n"
        unit = next(a for a in argv if a.startswith("--unit=")).split("=", 1)[1]
        self.units[unit] = {
            "active": "activating",
            "sub": "start",
            "argv": argv[argv.index("--") + 1 :],
        }
        return 0, ""

    def stop(self, unit):
        if self.on_stop:
            self.on_stop(unit)
        if self.stop_mode == "fail":
            return 1, "Failed to stop\n"
        if self.stop_mode == "outlasted":  # the command timed out; the stop went on
            self.units.pop(unit, None)
            self.sockets = [s for s in self.sockets if s["state"] != "0A"]
            self.write_net()
            return deploy.TIMED_OUT, "did not finish in time"
        if self.stop_mode == "slow":
            self.units[unit]["active"] = "deactivating"
            return 0, ""
        self.units.pop(unit, None)
        if self.stop_mode == "ok":
            self.sockets = [s for s in self.sockets if s["state"] != "0A"]
            self.write_net()
        return 0, ""

    # ---- the other fakes

    def make_lock(self, path):
        lock = FakeLock(self, path)
        self.locks_made.append(lock)
        return lock

    def aeri_runs(self):
        if self.aeri_error:
            raise OSError("registry")
        return list(self.aeri)

    # ---- driving a server

    def log_path(self):
        return self.state / "logs" / self.record()["log_file"]

    def log(self, *lines):
        path = self.log_path()
        with path.open("a") as handle:
            for line in lines:
                handle.write(line + "\n")

    def ckpt(self, model=SFT):
        return str(Path(self.site.checkpoints_root).resolve() / model)

    def norm_line(self, model=SFT):
        return (
            "INFO:root:Using norm stats shipped with the checkpoint: "
            f"{self.ckpt(model)}/norm_stats.json"
        )

    def ready_lines(self, model=SFT, rtc=True, cfg=False):
        lines = ["INFO:root:Loading model...", self.norm_line(model)]
        if cfg:
            lines.append(CFG_LINE)
        if rtc:
            lines += [RTC_CFG if cfg else RTC_ON, WARM]
        lines.append(LISTEN)
        return lines

    def server_comes_up(self, model=SFT, rtc=True, cfg=False, inode=4242, pid=777):
        """The unit's server loaded and listens, and said so."""
        unit = record.unit_name(model)
        self.units[unit]["active"], self.units[unit]["sub"] = "active", "running"
        self.log(*self.ready_lines(model, rtc, cfg))
        self.sockets.append(
            {"port": 8000, "state": "0A", "inode": inode, "proto": "tcp", "remote": 0}
        )
        self.write_net()
        self.own(unit, pid, [inode])

    # ---- driving the supervisor

    def deploy(self, **kw):
        return self.sup.deploy(body(**kw))

    def refused(self, call, *args, **kw):
        with pytest.raises(HTTPException) as caught:
            call(*args, **kw)
        detail = caught.value.detail
        return caught.value.status_code, detail["code"], detail


def body(model=SFT, **kw):
    value = {
        "request_id": REQ,
        "model_id": model,
        "rtc": True,
        "fraction": 0.22,
        "cfg_guidance_scale": None,
        "accept_unverified": False,
    }
    value.update(kw)
    value.setdefault("confirm", f"deploy {value['model_id']}")
    return value


@pytest.fixture
def marvel0(tmp_path, monkeypatch):
    return Marvel0(tmp_path, monkeypatch)


# --------------------------------------------------------------- a good deploy


def test_a_deploy_starts_the_unit_and_answers_starting(marvel0):
    answer = marvel0.deploy()
    assert answer["schema"] == "levi.marvel0.deploy_result.v1"
    assert (
        answer["created"],
        answer["replayed"],
        answer["phase"],
        answer["state"],
    ) == (
        True,
        False,
        "started",
        "starting",
    )
    assert answer["request_id"] == REQ
    assert answer["gpu"] == {
        "total_mib": TOTAL_MIB,
        "free_mib_at_final_check": TOTAL_MIB - 9100,
        "required_mib": 8198,
        "lock": "taken",
    }
    (run,) = marvel0.systemd_runs()
    unit = record.unit_name(SFT)
    assert run[:4] == ["systemd-run", "--user", "--quiet", f"--unit={unit}"]
    executed = run[run.index("--") + 1 :]
    assert executed == deploy.server_argv(
        marvel0.site, SFT_MODEL, True, None, marvel0.ckpt()
    )
    assert "--setenv=XLA_PYTHON_CLIENT_MEM_FRACTION=0.22" in run
    shown = answer["deploy"]
    assert (shown["unit"], shown["model_id"], shown["config"]) == (
        unit,
        SFT,
        "pi05_fr3_all_state",
    )
    assert (shown["rtc"], shown["fraction"], shown["phase"]) == (True, 0.22, "started")
    assert shown["gpu_lock"] == "taken"
    assert shown["source_hash"]["manifest"] is False
    assert sorted(shown["source_hash"]["files"]) == sorted(
        marvel0.site.source.hash_files
    )


def test_the_intent_is_on_disk_before_systemd_run_and_the_lock_is_held(marvel0):
    def look(argv):
        marvel0.record_at_run = marvel0.record()
        marvel0.held_at_run = [lock.held for lock in marvel0.locks_made]
        marvel0.log_exists = marvel0.log_path().exists()

    marvel0.on_systemd_run = look
    marvel0.deploy()
    intent = marvel0.record_at_run
    assert intent["phase"] == "intent"
    assert intent["unit"] == record.unit_name(SFT)
    assert intent["request_id"] == REQ and intent["model_id"] == SFT
    assert intent["config"] == "pi05_fr3_all_state" and intent["rtc"] is True
    assert intent["fraction"] == 0.22 and intent["checkpoint_dir"] == marvel0.ckpt()
    assert intent["exec_argv"][-1] == marvel0.ckpt()
    assert intent["systemd_run_argv"][0] == "systemd-run"
    assert (
        intent["exec_argv"]
        == intent["systemd_run_argv"][intent["systemd_run_argv"].index("--") + 1 :]
    )
    assert intent["gpu"]["required_mib"] == 8198
    assert intent["gpu_lock"] == "taken"
    assert set(intent["source_hash"]["files"]) == set(marvel0.site.source.hash_files)
    assert marvel0.held_at_run == [True], "the lock is held when systemd-run runs"
    assert marvel0.log_exists, "the log exists before the unit starts"
    assert marvel0.record()["phase"] == "started"
    names = [
        ("run", "systemd-run") if e[0] == "run" else e
        for e in marvel0.events
        if e[0] in ("record", "lock") or (e[0] == "run" and e[1][0] == "systemd-run")
    ]
    assert names.index(("record", "intent")) < names.index(("lock", "acquire"))
    assert names.index(("lock", "acquire")) < names.index(("run", "systemd-run"))
    assert names.index(("run", "systemd-run")) < names.index(("record", "started"))
    assert ("lock", "release") not in names, "held until the server is resident"


def test_no_lock_is_taken_before_the_final_check(marvel0):
    marvel0.deploy()
    first_lock = marvel0.events.index(("lock", "acquire"))
    assert not [
        e
        for e in marvel0.events[:first_lock]
        if e[0] == "run" and e[1][0] == "systemd-run"
    ]
    reads_after = [e for e in marvel0.events[first_lock:] if e[0] == "run"]
    assert reads_after[-1][1][0] == "systemd-run"
    assert reads_after[-2][1][0] == "nvidia-smi", (
        "the memory is read last, then the start"
    )


def test_the_gpu_lock_is_released_when_the_server_is_resident(marvel0):
    marvel0.deploy()
    lock = marvel0.locks_made[0]
    assert marvel0.sup.watch_once() is True and lock.held, "still loading"
    marvel0.clock.advance(3)
    assert marvel0.sup.watch_once() is True and lock.held
    marvel0.server_comes_up()
    marvel0.clock.advance(3)
    assert marvel0.sup.watch_once() is False
    assert not lock.held and marvel0.events.count(("lock", "release")) == 1
    assert marvel0.record()["lock_released_at"] and marvel0.record()["ready_at"]
    assert marvel0.sup.watch_once() is False, "nothing more to watch"


def test_the_lock_is_released_when_the_start_fails(marvel0):
    marvel0.deploy()
    marvel0.units.pop(record.unit_name(SFT))
    marvel0.log(*ARGPARSE)
    assert marvel0.sup.watch_once() is False
    assert not marvel0.locks_made[0].held


def test_the_lock_is_released_after_the_hold_limit(marvel0):
    marvel0.deploy()
    marvel0.clock.advance(marvel0.site.gpu.lock_hold_max_s - 1)
    assert marvel0.sup.watch_once() is True
    marvel0.clock.advance(2)
    assert marvel0.sup.watch_once() is False
    assert not marvel0.locks_made[0].held


def test_the_lock_is_released_after_three_unreadable_reads(marvel0):
    marvel0.deploy()
    marvel0.fail.add("show-state")
    assert marvel0.sup.watch_once() is True
    marvel0.clock.advance(3)
    assert marvel0.sup.watch_once() is True
    marvel0.clock.advance(3)
    assert marvel0.sup.watch_once() is False
    assert not marvel0.locks_made[0].held


def test_a_lock_that_is_not_ours_to_take_is_not_taken_when_live_holds_it(marvel0):
    marvel0.hold(marvel0.site.gpu_lock_file, 4001)
    marvel0.own(LIVE_UNIT, 4001)
    answer = marvel0.deploy()
    assert answer["gpu"]["lock"] == "live_holder"
    assert marvel0.locks_made == [], "the live vision model server keeps its lock"
    assert marvel0.record()["gpu_lock"] == "live_holder"
    assert marvel0.sup.watch_once() is False


def test_the_cfg_model_gets_its_guidance_flag(marvel0):
    marvel0.deploy(model=CFG, fraction=0.3, rtc=False, accept_unverified=True)
    (run,) = marvel0.systemd_runs()
    executed = run[run.index("--") + 1 :]
    assert executed[1] == "scripts/serve_policy.py"
    assert executed[2:6] == ["--port", "8000", "--cfg-guidance-scale", "1.0"]
    assert "--setenv=XLA_PYTHON_CLIENT_MEM_FRACTION=0.3" in run
    assert marvel0.record()["cfg_guidance_scale"] == 1.0


def test_an_explicit_guidance_scale_is_passed(marvel0):
    marvel0.deploy(model=CFG, accept_unverified=True, cfg_guidance_scale=1.25)
    (run,) = marvel0.systemd_runs()
    assert run[run.index("--cfg-guidance-scale") + 1] == "1.25"


def test_the_unit_environment_has_no_ros(marvel0):
    marvel0.env_text = "AMENT_PREFIX_PATH=/opt/ros\nROS_DISTRO=jazzy\nHOME=/h\n"
    marvel0.deploy()
    (run,) = marvel0.systemd_runs()
    unset = next(a for a in run if a.startswith("--property=UnsetEnvironment="))
    assert "AMENT_PREFIX_PATH" in unset and "ROS_DISTRO" in unset


def test_no_command_but_the_allowed_ones_is_ever_run(marvel0):
    marvel0.deploy()
    for argv, _ in marvel0.calls:
        assert argv[0] in ("systemctl", "systemd-run", "nvidia-smi")
        assert not {"kill", "pkill", "pgrep", "ss", "-9", "--signal"} & set(argv)


# --------------------------------------------------------------- the refusals
# Each of these alone refuses, with its code and status; nothing is started,
# no lock stays held and (before the intent) no record is left.


def assert_nothing_started(marvel0):
    assert marvel0.systemd_runs() == []
    assert not [lock for lock in marvel0.locks_made if lock.held]
    assert not marvel0.stops()


def refuse(marvel0, expect_status, expect_code, intent=False, **kw):
    """The deploy is refused with this status and code and nothing started. A
    refusal before the intent leaves no record; one after it (``intent``) marks
    the record aborted."""
    status, code, detail = marvel0.refused(marvel0.deploy, **kw)
    assert (status, code) == (expect_status, expect_code), detail
    assert_nothing_started(marvel0)
    assert (marvel0.record() or {}).get("phase") == ("aborted" if intent else None)
    return detail


def test_the_confirmation_phrase_must_name_the_model(marvel0):
    detail = refuse(marvel0, 400, "confirm_required", confirm="deploy something else")
    assert "deploy pi05_fr3_all_step49999" in detail["message"]
    refuse(marvel0, 400, "confirm_required", confirm="")
    assert marvel0.calls == []


@pytest.mark.parametrize(
    "request_id", ["short", "x" * 81, "has space 123", "../etc/passwd"]
)
def test_the_request_id_must_be_a_plain_token(marvel0, request_id):
    refuse(marvel0, 400, "request_id_invalid", request_id=request_id)
    assert marvel0.calls == []


def test_an_unknown_model_is_refused(marvel0):
    refuse(marvel0, 422, "model_unknown", model="nothing_here")


@pytest.mark.parametrize("fraction", [0.25, 0.5, 0.0, 1.0, 0.2201])
def test_only_the_site_presets_are_fractions(marvel0, fraction):
    refuse(marvel0, 422, "fraction_not_allowed", fraction=fraction)
    assert marvel0.calls == []


def test_a_preset_given_with_float_noise_is_accepted(marvel0):
    marvel0.deploy(fraction=0.2200001)


def test_a_guidance_scale_for_a_plain_model_is_refused(marvel0):
    refuse(marvel0, 422, "cfg_scale_not_allowed", cfg_guidance_scale=1.0)


@pytest.mark.parametrize("scale", [0.0, -1.0, 1.6])
def test_a_guidance_scale_out_of_range_is_refused(marvel0, scale):
    refuse(
        marvel0,
        422,
        "cfg_scale_out_of_range",
        model=CFG,
        accept_unverified=True,
        cfg_guidance_scale=scale,
    )


def test_an_unverified_model_needs_the_acceptance(marvel0):
    detail = refuse(marvel0, 409, "unverified_not_accepted", model=CFG)
    assert "real-GPU" in detail["message"]
    marvel0.deploy(model=CFG, accept_unverified=True)


def test_the_site_file_is_needed_before_anything_else(marvel0):
    def missing():
        raise HTTPException(412, {"code": "site_missing", "message": "x"})

    marvel0.sup.site_loader = missing
    assert marvel0.refused(marvel0.deploy)[:2] == (412, "site_missing")
    assert marvel0.refused(
        marvel0.sup.stop, {"request_id": REQ, "confirm": "stop policy server"}
    )[:2] == (
        412,
        "site_missing",
    )
    assert marvel0.refused(marvel0.sup.status)[:2] == (412, "site_missing")
    assert marvel0.refused(marvel0.sup.log, 10)[:2] == (412, "site_missing")
    assert marvel0.calls == []
    assert not (marvel0.tmp / "ws").exists(), "nothing was written"


def test_a_state_folder_inside_source_data_is_refused(tmp_path, monkeypatch):
    marvel0 = Marvel0(tmp_path, monkeypatch)
    marvel0.workspace = Workspace.at(Path(marvel0.site.rollout_root) / "ws")
    marvel0.sup.workspace = marvel0.workspace
    refuse(marvel0, 412, "state_dir_unusable")
    assert not Path(marvel0.site.rollout_root).exists()


@pytest.mark.parametrize("proto", ["tcp", "tcp6"])
def test_a_listener_on_the_port_refuses_the_deploy(marvel0, proto):
    marvel0.listen(proto=proto)
    detail = refuse(marvel0, 409, "port_taken")
    assert detail["listeners"] == 1 and detail["ours"] is False
    assert "Ctrl+C" in detail["message"]


def test_a_server_that_was_started_by_hand_is_never_touched(marvel0):
    marvel0.listen()
    marvel0.refused(marvel0.deploy)
    status, code, detail = marvel0.refused(
        marvel0.sup.stop, {"request_id": REQ, "confirm": "stop policy server"}
    )
    assert (status, code) == (409, "foreign_listener_not_stoppable")
    assert "Ctrl+C" in detail["message"]
    assert not marvel0.stops()


def test_an_unreadable_socket_table_is_not_a_free_port(marvel0):
    marvel0.fail.add("net-tcp")
    marvel0.write_net()
    refuse(marvel0, 503, "net_table_unreadable")


def test_a_connected_client_refuses_the_deploy(marvel0):
    marvel0.connect()
    detail = refuse(marvel0, 409, "client_connected")
    assert detail["established"] == 2


def test_a_client_on_another_machine_refuses_the_deploy(marvel0):
    marvel0.accept_from("10.0.0.9")
    detail = refuse(marvel0, 409, "client_connected")
    assert detail["established"] == 1


def test_an_outgoing_connection_to_port_8000_elsewhere_is_not_a_client(marvel0):
    marvel0.connect_out("203.0.113.7")
    answer = marvel0.deploy()
    assert (answer["created"], answer["phase"], answer["state"]) == (
        True,
        "started",
        "starting",
    )
    assert marvel0.systemd_runs(), "the deploy went on"


@pytest.mark.parametrize(
    "extra",
    [
        ["levi-aeri-policy-forward"],  # an AERI run's own policy unit
        ["levi-aeri-policy-manual-xyz.scope"],  # a hand-made scope
        ["levi-aeri-policy-pi05-fr3-all-step49999"],
    ],
)
def test_any_policy_unit_refuses_the_deploy(marvel0, extra):
    marvel0.foreign_units = extra
    detail = refuse(marvel0, 409, "policy_unit_exists")
    assert detail["units"] == [e if "." in e else f"{e}.service" for e in extra]


@pytest.mark.parametrize(
    "active", ["active", "activating", "inactive", "failed", "deactivating"]
)
def test_a_unit_of_ours_in_any_state_refuses_the_deploy(marvel0, active):
    for model in (SFT, CFG):  # a unit with our name is ours, whichever model
        marvel0.units.clear()
        marvel0.units[record.unit_name(model)] = {
            "active": active,
            "sub": "x",
            "argv": ["a"],
        }
        assert marvel0.refused(marvel0.deploy)[:2] == (409, "already_running")
    marvel0.foreign_units = ["levi-aeri-policy-forward"]
    assert marvel0.refused(marvel0.deploy)[:2] == (409, "policy_unit_exists"), (
        "ours and another"
    )
    assert_nothing_started(marvel0)


def test_an_unreadable_unit_list_refuses_the_deploy(marvel0):
    marvel0.fail.add("list-units")
    refuse(marvel0, 503, "systemd_unreadable")


def test_a_held_arm_lock_refuses_the_deploy_without_opening_the_lock(
    marvel0, monkeypatch
):
    marvel0.hold(marvel0.site.arm_lock_file, 5001)
    opened = []
    real_open = os.open
    monkeypatch.setattr(
        os,
        "open",
        lambda path, *a, **k: (opened.append(str(path)), real_open(path, *a, **k))[1],
    )
    refuse(marvel0, 409, "arm_lock_held")
    assert marvel0.site.arm_lock_file not in opened


def test_an_unreadable_lock_table_is_not_a_free_arm(marvel0):
    Path(marvel0.site.arm_lock_file).touch()
    marvel0.locks.unlink()
    refuse(marvel0, 503, "arm_lock_unknown")


def test_a_free_arm_lock_file_does_not_refuse(marvel0):
    Path(marvel0.site.arm_lock_file).touch()
    marvel0.deploy()


def test_a_live_standalone_run_refuses_the_deploy(marvel0):
    marvel0.aeri = [{"run_id": "r-live", "backend": "standalone", "runner_alive": True}]
    detail = refuse(marvel0, 409, "aeri_run_live")
    assert detail["run_id"] == "r-live"


def test_runs_that_are_not_live_standalone_runs_do_not_refuse(marvel0):
    marvel0.aeri = [
        {"run_id": "r-old", "backend": "standalone", "runner_alive": False},
        {"run_id": "r-systemd", "backend": "systemd", "runner_alive": True},
    ]
    marvel0.deploy()


def test_an_unreadable_run_registry_refuses_the_deploy(marvel0):
    marvel0.aeri_error = True
    refuse(marvel0, 503, "aeri_registry_unreadable")


def test_a_foreign_gpu_lock_holder_refuses_the_deploy(marvel0):
    marvel0.hold(marvel0.site.gpu_lock_file, 6001)
    refuse(marvel0, 409, "gpu_lock_foreign")
    assert marvel0.locks_made == []


def test_a_holder_that_is_only_partly_the_live_server_is_foreign(marvel0):
    marvel0.hold(marvel0.site.gpu_lock_file, 4001)
    marvel0.hold(marvel0.site.gpu_lock_file, 6001)
    marvel0.own(LIVE_UNIT, 4001)
    refuse(marvel0, 409, "gpu_lock_foreign")


def test_an_unreadable_lock_table_is_a_foreign_gpu_holder(marvel0):
    Path(marvel0.site.gpu_lock_file).parent.mkdir(parents=True, exist_ok=True)
    Path(marvel0.site.gpu_lock_file).touch()
    marvel0.locks.unlink()
    refuse(marvel0, 409, "gpu_lock_foreign")


def test_a_live_unit_that_cannot_be_read_does_not_exempt_a_holder(marvel0):
    marvel0.hold(marvel0.site.gpu_lock_file, 4001)
    marvel0.fail.add("show-cgroup")
    refuse(marvel0, 409, "gpu_lock_foreign")


def test_a_busy_lock_at_the_final_check_refuses_the_deploy(marvel0):
    marvel0.lock_busy = True
    refuse(marvel0, 409, "gpu_lock_foreign", intent=True)
    assert marvel0.record()["phase"] == "aborted"


def test_an_unreadable_user_manager_environment_refuses_the_deploy(marvel0):
    marvel0.fail.add("show-environment")
    refuse(marvel0, 503, "env_unreadable")


@pytest.mark.parametrize("text", ["", "garbage\n", "100, 32607\n200, 32607\n"])
def test_an_unreadable_gpu_refuses_the_deploy(marvel0, text):
    marvel0.gpu_text = text
    refuse(marvel0, 503, "gpu_unreadable")
    marvel0.gpu_text = None
    marvel0.fail.add("nvidia-smi")
    assert marvel0.refused(marvel0.deploy)[1] == "gpu_unreadable"


@pytest.mark.parametrize("fraction, needed", sorted(FRACTIONS.items()))
def test_free_memory_must_cover_the_fraction_and_the_margin(marvel0, fraction, needed):
    marvel0.gpu = [TOTAL_MIB - (needed - 1), TOTAL_MIB]  # one MiB short
    detail = refuse(
        marvel0, 409, "gpu_insufficient", fraction=fraction, request_id="req-short-0001"
    )
    assert (detail["total_mib"], detail["free_mib"]) == (TOTAL_MIB, needed - 1)
    assert (detail["required_mib"], detail["margin_mib"]) == (needed, 1024)
    assert str(needed) in detail["message"] and str(TOTAL_MIB) in detail["message"]
    assert "live vLLM" in detail["message"]
    marvel0.gpu = [TOTAL_MIB - needed, TOTAL_MIB]  # exactly enough
    answer = marvel0.deploy(fraction=fraction, request_id="req-exact-0001")
    assert answer["gpu"]["required_mib"] == needed


def test_the_wording_of_the_memory_refusal(marvel0):
    marvel0.gpu = [TOTAL_MIB - 5100, TOTAL_MIB]
    detail = refuse(marvel0, 409, "gpu_insufficient", fraction=0.3)
    assert detail["message"] == (
        "Fraction 0.30 of the 32607 MiB card needs 10807 MiB free (9783 + 1024); "
        "5100 MiB are free. Put the live vLLM to sleep or stop it (/live), then try again."
    )


@pytest.mark.parametrize(
    "missing, code",
    [
        ("python", "runtime_files_missing"),
        ("not_executable", "runtime_files_missing"),
        ("rtc_script", "runtime_files_missing"),
        ("checkpoint", "checkpoint_invalid"),
        ("params", "checkpoint_invalid"),
        ("norm", "checkpoint_invalid"),
        ("assets_only", "checkpoint_invalid"),
        ("escaping_link", "checkpoint_invalid"),
    ],
)
def test_the_files_a_start_needs_must_be_there(marvel0, missing, code):
    ckpt = Path(marvel0.ckpt())
    scripts = Path(marvel0.site.openpi_checkout, "scripts")
    if missing == "python":
        Path(marvel0.site.python).unlink()
    elif missing == "not_executable":
        Path(marvel0.site.python).chmod(0o644)
    elif missing == "rtc_script":
        (scripts / "serve_policy_rtc.py").unlink()
    elif missing == "checkpoint":
        shutil.rmtree(ckpt)
    elif missing == "params":
        shutil.rmtree(ckpt / "params")
    elif missing == "norm":
        (ckpt / "norm_stats.json").unlink()
    elif missing == "assets_only":
        (ckpt / "norm_stats.json").unlink()
        (ckpt / "assets").mkdir(exist_ok=True)
        (ckpt / "assets" / "norm_stats.json").write_text("{}")
    elif missing == "escaping_link":
        outside = marvel0.tmp / "outside" / SFT
        outside.parent.mkdir()
        shutil.move(str(ckpt), str(outside))
        ckpt.symlink_to(outside)
    refuse(marvel0, 412, code)
    assert marvel0.record() is None


def test_the_plain_server_needs_its_own_script(tmp_path, monkeypatch):
    hashed = ["scripts/serve_policy_rtc.py", "src/openpi/policies/rtc_policy.py"]
    marvel0 = Marvel0(tmp_path, monkeypatch, source={"hash_files": hashed})
    Path(marvel0.site.openpi_checkout, "scripts", "serve_policy.py").unlink()
    status, code, _ = marvel0.refused(marvel0.deploy, rtc=False)
    assert (status, code) == (412, "runtime_files_missing")
    marvel0.deploy(request_id="req-000002-bbbb")  # RTC: the RTC script is there


def test_a_missing_source_file_refuses_the_deploy(marvel0):
    Path(marvel0.site.openpi_checkout, marvel0.site.source.hash_files[2]).unlink()
    refuse(marvel0, 412, "source_file_missing")


def test_a_changed_source_file_refuses_when_a_manifest_names_it(tmp_path, monkeypatch):
    marvel0 = Marvel0(tmp_path, monkeypatch, source={"manifest": "manifest.json"})
    files = {
        rel: hashlib.sha256(
            Path(marvel0.site.openpi_checkout, rel).read_bytes()
        ).hexdigest()
        for rel in marvel0.site.source.hash_files
    }
    manifest = Path(marvel0.site.openpi_checkout, "manifest.json")
    manifest.write_text(json.dumps({"files": files}))
    marvel0.deploy()
    assert marvel0.record()["source_hash"]["manifest"] is True
    marvel0.units.clear()
    marvel0.sockets.clear()
    marvel0.write_net()
    Path(marvel0.site.openpi_checkout, marvel0.site.source.hash_files[0]).write_text(
        "# edited\n"
    )
    status, code, detail = marvel0.refused(marvel0.deploy, request_id="req-000002-bbbb")
    assert (status, code) == (409, "source_changed")
    assert detail["file"] == marvel0.site.source.hash_files[0]
    assert len(marvel0.systemd_runs()) == 1


def test_without_a_manifest_the_hashes_are_only_recorded(marvel0):
    marvel0.deploy()
    files = marvel0.record()["source_hash"]
    assert files["manifest"] is False
    for rel, sha in files["files"].items():
        assert (
            sha
            == hashlib.sha256(
                Path(marvel0.site.openpi_checkout, rel).read_bytes()
            ).hexdigest()
        )


# --------------------------------------------------------------- the races


def test_a_port_taken_between_the_checks_aborts_after_the_intent(marvel0):
    marvel0.on_acquire = lambda: marvel0.listen()
    status, code, _ = marvel0.refused(marvel0.deploy)
    assert (status, code) == (409, "port_taken")
    assert marvel0.record()["phase"] == "aborted"
    assert marvel0.record()["abort_code"] == "port_taken"
    assert_nothing_started(marvel0)
    assert ("lock", "release") in marvel0.events


def test_a_client_appearing_between_the_checks_aborts_the_deploy(marvel0):
    marvel0.on_acquire = lambda: marvel0.connect()
    assert marvel0.refused(marvel0.deploy)[1] == "client_connected"
    assert marvel0.record()["phase"] == "aborted"


def test_a_unit_appearing_between_the_checks_aborts_the_deploy(marvel0):
    marvel0.on_acquire = lambda: marvel0.foreign_units.append(
        "levi-aeri-policy-forward"
    )
    assert marvel0.refused(marvel0.deploy)[1] == "policy_unit_exists"
    assert marvel0.record()["phase"] == "aborted"
    assert_nothing_started(marvel0)


def test_the_arm_taken_between_the_checks_aborts_the_deploy(marvel0):
    marvel0.on_acquire = lambda: marvel0.hold(marvel0.site.arm_lock_file, 5001)
    assert marvel0.refused(marvel0.deploy)[1] == "arm_lock_held"
    assert_nothing_started(marvel0)


def test_memory_lost_between_the_checks_aborts_the_deploy(marvel0):
    def eat():
        marvel0.gpu = [TOTAL_MIB - 3000, TOTAL_MIB]

    marvel0.on_acquire = eat
    status, code, detail = marvel0.refused(marvel0.deploy)
    assert (status, code, detail["free_mib"]) == (409, "gpu_insufficient", 3000)
    assert marvel0.record()["phase"] == "aborted"
    assert_nothing_started(marvel0)


def test_an_aborted_deploy_does_not_leave_the_state_in_the_way(marvel0):
    marvel0.on_acquire = lambda: marvel0.listen()
    marvel0.refused(marvel0.deploy)
    status = marvel0.sup.status()
    assert status["state"] == "foreign_listener"
    marvel0.sockets.clear()
    marvel0.write_net()
    marvel0.on_acquire = None
    marvel0.sup.invalidate()
    assert marvel0.sup.status()["state"] == "idle"
    marvel0.deploy(request_id="req-000002-bbbb")


# --------------------------------------------------------------- requests


def test_the_same_request_is_answered_again_and_never_acted_on_twice(marvel0):
    first = marvel0.deploy()
    second = marvel0.deploy(confirm="deploy pi05_fr3_all_step49999")
    assert len(marvel0.systemd_runs()) == 1
    assert second["replayed"] is True and first["replayed"] is False
    assert {k: v for k, v in second.items() if k != "replayed"} == {
        k: v for k, v in first.items() if k != "replayed"
    }


def test_a_request_is_answered_again_even_if_the_catalog_changed(marvel0):
    first = marvel0.deploy()
    marvel0.catalog.models.clear()  # the checkpoint was removed meanwhile
    again = marvel0.deploy()
    assert again["replayed"] is True and again["deploy"] == first["deploy"]
    assert (
        marvel0.refused(marvel0.deploy, request_id="req-000002-bbbb")[1]
        == "model_unknown"
    )


def test_a_refused_form_is_in_the_audit_too(marvel0):
    marvel0.refused(marvel0.deploy, fraction=0.25)
    lines = [
        json.loads(line)
        for line in (marvel0.state / "history.jsonl").read_text().splitlines()
    ]
    assert [(l["phase"], l["code"]) for l in lines] == [
        ("refused", "fraction_not_allowed")
    ]


def test_a_replay_survives_a_restart(marvel0):
    first = marvel0.deploy()
    again = deploy.Supervisor(
        marvel0.host,
        site_loader=lambda: marvel0.site,
        catalog=marvel0.catalog,
        workspace=marvel0.workspace,
    )
    second = again.deploy(body())
    assert second["replayed"] is True and second["deploy"] == first["deploy"]
    assert len(marvel0.systemd_runs()) == 1


def test_a_request_id_cannot_be_reused_for_another_deploy(marvel0):
    marvel0.deploy()
    status, code, _ = marvel0.refused(marvel0.deploy, rtc=False)
    assert (status, code) == (409, "request_id_used")
    assert (
        marvel0.refused(marvel0.deploy, model=CFG, accept_unverified=True)[1]
        == "request_id_used"
    )
    assert len(marvel0.systemd_runs()) == 1


def test_a_refusal_is_not_stored_so_the_same_id_can_be_tried_again(marvel0):
    marvel0.listen()
    assert marvel0.refused(marvel0.deploy)[1] == "port_taken"
    marvel0.sockets.clear()
    marvel0.write_net()
    answer = marvel0.deploy()
    assert answer["replayed"] is False and len(marvel0.systemd_runs()) == 1


def test_a_request_that_crashed_after_it_was_accepted_starts_nothing(marvel0):
    directory = record.state_dir((), marvel0.workspace)
    fp = record.fingerprint("deploy", body())
    record.put_request(directory, REQ, {"status": "accepted", "fingerprint": fp})
    answer = marvel0.deploy()
    assert (answer["state"], answer["replayed"]) == ("unknown", True)
    assert answer["reason"]["code"] == "request_incomplete"
    assert marvel0.systemd_runs() == []


def test_systemd_run_failing_is_a_clean_refusal(marvel0):
    marvel0.fail.add("systemd-run")
    status, code, detail = marvel0.refused(marvel0.deploy)
    assert (status, code, detail["exit_code"]) == (502, "start_failed", 1)
    assert "Unknown assignment" in detail["detail"]
    assert marvel0.record()["phase"] == "start_failed"
    assert not marvel0.locks_made[0].held
    marvel0.fail.clear()
    marvel0.deploy()  # the same request id may try again: nothing ran


@pytest.mark.parametrize("how", ["raises", "times_out"])
def test_a_systemd_run_that_may_have_started_the_unit_is_not_called_failed(
    marvel0, how
):
    if how == "raises":
        marvel0.run_raises = RuntimeError("boom")
    else:
        marvel0.systemd_run_answer = (deploy.TIMED_OUT, "did not finish in time")
    status, code, detail = marvel0.refused(marvel0.deploy)
    assert (status, code) == (502, "start_failed")
    assert "may or may not have started" in detail["message"]
    assert detail["exit_code"] is None
    # The record stays at intent so a unit that appears is followed and can be
    # stopped; the GPU lock stays held until the server is resident or gone.
    assert (
        marvel0.record()["phase"] == "intent" and marvel0.record()["start_unconfirmed"]
    )
    assert marvel0.locks_made[0].held
    assert marvel0.sup.watch_once() is True
    # Sending the request again starts nothing.
    again = marvel0.deploy()
    assert again["reason"]["code"] == "request_incomplete"
    assert len(marvel0.systemd_runs()) == 1
    # No unit appeared: after the grace it is a failure, and the lock is let go.
    assert marvel0.sup.status()["state"] == "starting"
    marvel0.clock.advance(deploy.INTENT_GRACE_S + 1)
    marvel0.sup.invalidate()
    view = marvel0.sup.status()
    assert (view["state"], view["failure"]["kind"]) == ("failed", "intent_only")
    assert marvel0.sup.watch_once() is False and not marvel0.locks_made[0].held


def test_a_unit_that_appears_after_an_unanswered_systemd_run_is_followed(marvel0):
    marvel0.systemd_run_answer = (deploy.TIMED_OUT, "did not finish in time")
    marvel0.refused(marvel0.deploy)
    unit = record.unit_name(SFT)
    marvel0.units[unit] = {
        "active": "activating",
        "sub": "start",
        "argv": marvel0.record()["exec_argv"],
    }
    marvel0.sup.invalidate()
    assert marvel0.sup.status()["state"] == "starting"
    marvel0.log_path().write_text("")  # (create_log made it before the command ran)
    marvel0.server_comes_up()
    marvel0.sup.invalidate()
    assert marvel0.sup.status()["state"] == "ready"
    assert marvel0.sup.watch_once() is False, "resident: the lock is let go"
    assert stop(marvel0)["stopped"] is True, "and the page can stop what it started"


def test_a_log_file_that_cannot_be_made_is_a_clean_failure(marvel0, monkeypatch):
    def taken(directory, unit, now):
        raise FileExistsError("taken")

    monkeypatch.setattr(record, "create_log", taken)
    status, code, _ = marvel0.refused(marvel0.deploy)
    assert (status, code) == (502, "start_failed")
    assert marvel0.record()["phase"] == "start_failed" and marvel0.systemd_runs() == []
    assert not marvel0.locks_made[0].held


def test_another_deploy_waits_for_an_unanswered_systemd_run(marvel0):
    # systemd-run did not answer: its unit may still be created. A second deploy
    # (another model, a new request id) would put two servers on the port.
    marvel0.systemd_run_answer = (deploy.TIMED_OUT, "did not finish in time")
    marvel0.refused(marvel0.deploy)
    first = marvel0.locks_made[0]
    assert first.held
    marvel0.systemd_run_answer = None
    marvel0.clock.advance(1)
    status, code, _ = marvel0.refused(
        marvel0.deploy, model=CFG, request_id="req-000002-bbbb", accept_unverified=True
    )
    assert (status, code) == (409, "start_unconfirmed")
    assert len(marvel0.systemd_runs()) == 1, "no second systemd-run"
    assert first.held and len(marvel0.locks_made) == 1, (
        "the lock was neither dropped nor taken"
    )
    assert marvel0.record()["unit"] == record.unit_name(SFT), "the record is untouched"
    # The refusal is not stored: the same id may try once the state has settled.
    # No unit appeared; after the grace the page says failed and a deploy may go on.
    marvel0.clock.advance(deploy.INTENT_GRACE_S)
    marvel0.sup.invalidate()
    assert marvel0.sup.status()["state"] == "failed"
    answer = marvel0.deploy(
        model=CFG, request_id="req-000002-bbbb", accept_unverified=True
    )
    assert answer["state"] == "starting" and len(marvel0.systemd_runs()) == 2
    assert not first.held, "the lock of the call that never answered is let go then"


def test_a_plain_intent_that_has_not_settled_also_holds_a_new_deploy_back(marvel0):
    # The core died between the intent and systemd-run (no start_unconfirmed).
    marvel0.deploy()
    record.update_record(marvel0.state, phase="intent")
    marvel0.units.clear()
    marvel0.sockets.clear()
    marvel0.write_net()
    marvel0.sup.invalidate()
    assert marvel0.sup.status()["state"] == "starting"
    status, code, _ = marvel0.refused(marvel0.deploy, request_id="req-000002-bbbb")
    assert (status, code) == (409, "start_unconfirmed")


def test_one_operation_runs_at_a_time(marvel0):
    entered, release = threading.Event(), threading.Event()

    def hold_run(argv):
        entered.set()
        assert release.wait(10)

    marvel0.on_systemd_run = hold_run
    results = []
    worker = threading.Thread(target=lambda: results.append(marvel0.deploy()))
    worker.start()
    try:
        assert entered.wait(10)
        status, code, detail = marvel0.refused(
            marvel0.deploy, request_id="req-000002-bbbb"
        )
        assert (status, code) == (423, "busy")
        assert detail["operation"]["action"] == "deploy"
        assert detail["operation"]["request_id"] == REQ
        status, code, _ = marvel0.refused(
            marvel0.sup.stop,
            {"request_id": "req-000003-cccc", "confirm": "stop policy server"},
        )
        assert (status, code) == (423, "busy")
        assert marvel0.sup.status()["operation"]["action"] == "deploy"
    finally:
        release.set()
        worker.join(10)
    assert results and len(marvel0.systemd_runs()) == 1
    assert marvel0.sup.status()["operation"] is None


def test_a_finished_request_is_answered_even_while_another_operation_runs(marvel0):
    running(marvel0)
    first = stop(marvel0)  # request req-stop-0001, finished
    entered, release = threading.Event(), threading.Event()

    def hold_run(argv):
        entered.set()
        assert release.wait(10)

    marvel0.on_systemd_run = hold_run
    worker = threading.Thread(
        target=lambda: marvel0.deploy(request_id="req-000002-bbbb")
    )
    worker.start()
    try:
        assert entered.wait(10)
        again = stop(marvel0)  # not 423: it is a request that was already answered
        assert again["replayed"] is True and again["stopped"] is first["stopped"]
        assert marvel0.refused(stop, marvel0, request_id="req-stop-0002")[1] == "busy"
    finally:
        release.set()
        worker.join(10)


def test_another_process_holding_the_operation_lock_makes_it_busy(marvel0):
    directory = record.state_dir((), marvel0.workspace)
    fd = os.open(directory / "op.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status, code, detail = marvel0.refused(marvel0.deploy)
        assert (status, code) == (423, "busy") and detail["operation"] is None
    finally:
        os.close(fd)
    assert marvel0.systemd_runs() == []
    marvel0.deploy()


def test_audit_lines_tell_what_happened(marvel0):
    marvel0.listen()
    marvel0.refused(marvel0.deploy)
    marvel0.sockets.clear()
    marvel0.write_net()
    marvel0.deploy(request_id="req-000002-bbbb")
    lines = [
        json.loads(line)
        for line in (marvel0.state / "history.jsonl").read_text().splitlines()
    ]
    phases = [(l["action"], l["phase"], l.get("code")) for l in lines]
    assert ("deploy", "refused", "port_taken") in phases
    assert ("deploy", "accepted", None) in phases
    assert ("deploy", "started", None) in phases
    assert all(l["actor"] == "person" and l["via"] == "product" for l in lines)
    assert str(marvel0.tmp) not in (marvel0.state / "history.jsonl").read_text()


# ====================================================================== part 3
# Stop, state and log.

STOP = {"request_id": "req-stop-0001", "confirm": "stop policy server"}
ABSOLUTE = re.compile(r"(?<![\w.:-])/(?:[A-Za-z0-9_.~+@%=,-]+/)+")


def running(marvel0, ready=True, **kw):
    """A deployed server, up and listening unless ``ready`` is False."""
    answer = marvel0.deploy(**kw)
    if ready:
        marvel0.server_comes_up(**{k: v for k, v in kw.items() if k in ("rtc", "cfg")})
    marvel0.sup.invalidate()
    return answer


def stop(marvel0, **kw):
    return marvel0.sup.stop({**STOP, **kw})


def assert_no_paths(marvel0, value):
    text = json.dumps(value)
    assert str(marvel0.tmp) not in text
    assert not ABSOLUTE.search(text), ABSOLUTE.search(text).group(0)


# --------------------------------------------------------------- stop


def test_a_running_server_is_stopped_by_its_own_unit_only(marvel0):
    running(marvel0)
    assert marvel0.sup.status()["state"] == "ready"
    unit = record.unit_name(SFT)
    seen = {}
    marvel0.on_stop = lambda name: seen.update(
        phase=marvel0.record()["phase"], unit=name
    )
    answer = stop(marvel0)
    assert seen == {"phase": "stopping", "unit": unit}, "marked before the command"
    assert marvel0.stops() == [["systemctl", "--user", "stop", f"{unit}.service"]]
    assert [t for a, t in marvel0.calls if a[2:3] == ["stop"]] == [90 + 30]
    assert answer == {
        "schema": "levi.marvel0.stop_result.v1",
        "created": True,
        "replayed": False,
        "request_id": STOP["request_id"],
        "state": "idle",
        "unit": unit,
        "stopped": True,
        "client_override_used": False,
        "next": deploy.NEXT_STOP,
    }
    assert marvel0.record()["phase"] == "stopped"
    assert marvel0.sup.status()["state"] == "idle"
    assert_no_paths(marvel0, answer)


def test_the_unit_is_identified_before_the_stop_command(marvel0):
    running(marvel0)
    stop(marvel0)
    commands = marvel0.commands()
    show = next(
        i
        for i, c in enumerate(commands)
        if c[2:4] == ("show", "-p") and "ExecStart" in c
    )
    assert show < commands.index(tuple(marvel0.stops()[0]))
    assert "Id" in commands[show] and commands[show][-1].endswith(".service")


def test_a_stop_while_the_server_is_still_loading_is_allowed(marvel0):
    running(marvel0, ready=False)
    assert stop(marvel0)["stopped"] is True
    assert marvel0.units == {}


def test_a_stop_releases_the_gpu_lock_it_still_holds(marvel0):
    running(marvel0, ready=False)
    lock = marvel0.locks_made[0]
    assert lock.held
    stop(marvel0)
    assert not lock.held
    assert marvel0.sup.watch_once() is False


def test_the_same_stop_request_is_answered_again(marvel0):
    running(marvel0)
    first = stop(marvel0)
    again = stop(marvel0)
    assert len(marvel0.stops()) == 1
    assert again["replayed"] is True and again["stopped"] is True
    assert {k: v for k, v in again.items() if k != "replayed"} == {
        k: v for k, v in first.items() if k != "replayed"
    }
    status, code, _ = marvel0.refused(
        stop, marvel0, client_override="stop with client connected"
    )
    assert (status, code) == (409, "request_id_used")


def test_a_wrong_phrase_stops_nothing(marvel0):
    running(marvel0)
    before = len(marvel0.calls)
    for bad in ("", "stop", "Stop policy server", "stop policy server "):
        assert marvel0.refused(stop, marvel0, confirm=bad)[:2] == (
            400,
            "confirm_required",
        )
    assert marvel0.refused(stop, marvel0, request_id="x")[:2] == (
        400,
        "request_id_invalid",
    )
    assert marvel0.refused(stop, marvel0, client_override="yes please")[:2] == (
        400,
        "confirm_required",
    )
    assert len(marvel0.calls) == before and not marvel0.stops()


def test_a_unit_that_is_not_the_recorded_one_is_not_stopped(marvel0):
    running(marvel0)
    unit = record.unit_name(SFT)
    marvel0.units[unit]["argv"] = [
        *marvel0.units[unit]["argv"][:-1],
        "/other/checkpoint",
    ]
    status, code, _ = marvel0.refused(stop, marvel0)
    assert (status, code) == (409, "unit_mismatch")
    assert not marvel0.stops()
    assert marvel0.record()["phase"] == "started", "nothing was changed"


def test_a_record_naming_another_unit_stops_nothing(marvel0):
    running(marvel0)
    record.update_record(marvel0.state, unit="levi-aeri-policy-forward")
    assert marvel0.refused(stop, marvel0)[1] == "unit_mismatch"
    record.update_record(marvel0.state, unit=record.unit_name(SFT), exec_argv=["x"])
    assert (
        marvel0.refused(stop, marvel0, request_id="req-stop-0002")[1] == "unit_mismatch"
    )
    assert not marvel0.stops()


def test_a_listener_that_is_not_the_units_is_never_stopped(marvel0):
    running(marvel0)
    marvel0.sockets = [s for s in marvel0.sockets if s["state"] != "0A"]
    marvel0.listen(inode=555)  # someone else's server holds the port
    status, code, detail = marvel0.refused(stop, marvel0)
    assert (status, code) == (409, "foreign_listener_not_stoppable")
    assert "Ctrl+C" in detail["message"]
    assert not marvel0.stops()


def test_a_server_with_no_record_is_nothing_to_stop(marvel0):
    assert marvel0.refused(stop, marvel0)[:2] == (409, "nothing_to_stop")
    marvel0.listen()
    assert marvel0.refused(stop, marvel0, request_id="req-stop-0002")[:2] == (
        409,
        "foreign_listener_not_stoppable",
    )
    assert not marvel0.stops()


def test_a_server_that_is_already_gone_is_recorded_as_stopped(marvel0):
    running(marvel0)
    marvel0.units.clear()
    marvel0.sockets.clear()
    marvel0.write_net()
    answer = stop(marvel0)
    assert (answer["state"], answer["stopped"], answer["already"]) == (
        "idle",
        False,
        True,
    )
    assert not marvel0.stops()
    assert marvel0.record()["phase"] == "stopped"


def test_an_unreadable_state_stops_nothing(marvel0):
    running(marvel0)
    marvel0.fail.add("show-state")
    assert marvel0.refused(stop, marvel0)[:2] == (503, "systemd_unreadable")
    marvel0.fail.clear()
    marvel0.fail.add("net-tcp")
    marvel0.write_net()
    assert marvel0.refused(stop, marvel0, request_id="req-stop-0002")[:2] == (
        503,
        "net_table_unreadable",
    )
    assert not marvel0.stops()


def test_a_connected_client_stops_the_stop(marvel0):
    running(marvel0)
    marvel0.connect()
    status, code, detail = marvel0.refused(stop, marvel0)
    assert (status, code, detail["established"]) == (409, "client_connected", 2)
    assert not marvel0.stops()
    assert marvel0.record()["phase"] == "started"
    # Refused by the first look: the record was never marked, nothing waited.
    assert ("record", "stopping") not in marvel0.events and marvel0.clock.sleeps == []


def test_the_typed_phrase_overrides_a_connected_client(marvel0):
    running(marvel0)
    marvel0.connect()
    answer = stop(marvel0, client_override="stop with client connected")
    assert answer["stopped"] is True and answer["client_override_used"] is True
    assert "e-stop" in answer["warning"], "the client has lost its server"
    assert len(marvel0.stops()) == 1


def test_a_stop_without_a_client_carries_no_warning(marvel0):
    running(marvel0)
    assert "warning" not in stop(marvel0, client_override="stop with client connected")


def test_the_hand_notice_outlasts_a_stop_that_cut_a_client_off(marvel0):
    marvel0.robot = {"marker": "present", "open_session": True}
    running(marvel0)
    marvel0.connect()
    stop(marvel0, client_override="stop with client connected")
    marvel0.disconnect()
    marvel0.sup.invalidate()
    view = marvel0.sup.status()
    assert view["state"] == "idle"
    assert view["estop"]["keep_hand"] is True, "the answer's warning is gone on reload"
    # It stays until the arm controller is gone and no session is open.
    marvel0.robot = {"marker": "absent", "open_session": True}
    marvel0.sup.invalidate()
    assert marvel0.sup.status()["estop"]["keep_hand"] is True
    marvel0.robot = {"marker": "absent", "open_session": False}
    marvel0.sup.invalidate()
    assert marvel0.sup.status()["estop"]["keep_hand"] is False


def test_a_stop_without_a_client_leaves_no_notice_and_a_new_deploy_clears_it(marvel0):
    marvel0.robot = {"marker": "present", "open_session": True}
    running(marvel0)
    stop(marvel0)
    marvel0.sup.invalidate()
    assert marvel0.sup.status()["estop"]["keep_hand"] is False
    running(marvel0, request_id="req-000002-bbbb")
    marvel0.connect()
    stop(
        marvel0,
        request_id="req-stop-0002",
        client_override="stop with client connected",
    )
    marvel0.disconnect()
    assert marvel0.record()["stopped_with_client"] is True
    marvel0.deploy(request_id="req-000003-cccc")
    assert "stopped_with_client" not in marvel0.record(), (
        "the next deploy starts afresh"
    )


def test_a_client_arriving_during_the_pause_stops_the_stop(marvel0):
    running(marvel0)
    marvel0.clock.on_sleep = lambda seconds: marvel0.connect()
    status, code, _ = marvel0.refused(stop, marvel0)
    assert (status, code) == (409, "client_connected")
    assert not marvel0.stops()
    assert marvel0.record()["phase"] == "started", "put back as it was"
    assert marvel0.clock.sleeps[0] == pytest.approx(0.3)
    assert marvel0.sup.status()["state"] == "ready"


def test_the_client_check_is_made_twice_with_a_pause_between(marvel0):
    running(marvel0)
    reads = []
    real_read = deploy.net.read
    deploy.net.read = lambda *a, **k: (
        reads.append(marvel0.clock.t),
        real_read(*a, **k),
    )[1]
    try:
        stop(marvel0)
    finally:
        deploy.net.read = real_read
    assert len(reads) >= 2 and reads[1] - reads[0] == pytest.approx(0.3)


def test_a_failed_stop_command_puts_the_record_back(marvel0):
    running(marvel0)
    marvel0.stop_mode = "fail"
    status, code, detail = marvel0.refused(stop, marvel0)
    assert (status, code, detail["exit_code"]) == (502, "stop_failed", 1)
    assert marvel0.record()["phase"] == "started"
    assert marvel0.sup.status()["state"] == "ready"


def test_a_stop_command_that_outlasts_its_timeout_is_judged_by_the_wait(marvel0):
    running(marvel0)
    marvel0.stop_mode = "outlasted"
    answer = stop(marvel0)
    assert (answer["state"], answer["stopped"]) == ("idle", True)
    assert marvel0.record()["phase"] == "stopped"


def test_a_stop_that_takes_too_long_is_stopping_and_then_incomplete(marvel0):
    running(marvel0)
    unit = record.unit_name(SFT)
    marvel0.stop_mode = "slow"
    answer = stop(marvel0)
    assert (answer["state"], answer["stopped"]) == ("stopping", False)
    assert sum(marvel0.clock.sleeps) >= 90 + 30 - 1, "waited the stop timeout and more"
    assert marvel0.record()["phase"] == "stopping"
    assert marvel0.sup.status()["state"] == "stopping"
    status, code, detail = marvel0.refused(stop, marvel0, request_id="req-stop-0002")
    assert (status, code) == (502, "stop_incomplete")
    assert f"systemctl --user stop {unit}" in detail["message"]
    # The person finishes it by hand; a later stop sees the unit gone.
    marvel0.units.clear()
    marvel0.sockets.clear()
    marvel0.write_net()
    again = stop(marvel0, request_id="req-stop-0003")
    assert again["already"] is True
    assert marvel0.record()["phase"] == "stopped"


def test_a_unit_that_is_gone_while_the_port_still_listens_is_incomplete(marvel0):
    running(marvel0)
    marvel0.stop_mode = "port_stays"
    status, code, _ = marvel0.refused(stop, marvel0)
    assert (status, code) == (502, "stop_incomplete")
    assert marvel0.sup.status()["state"] == "foreign_listener"


def test_a_stopping_mark_left_by_a_dead_core_can_be_stopped_again(marvel0):
    # The core stopped between the "stopping" mark and the stop command: the
    # unit is still active, no stop is going on, so the page must offer one.
    running(marvel0)
    record.update_record(marvel0.state, phase="stopping", stopping_from="started")
    marvel0.sup.invalidate()
    view = marvel0.sup.status()
    assert (view["state"], view["can_stop"]) == ("stopping", True)
    answer = stop(marvel0)
    assert (answer["state"], answer["stopped"]) == ("idle", True)
    assert marvel0.record()["phase"] == "stopped"


def test_no_second_stop_is_offered_while_one_is_going_on(marvel0):
    running(marvel0)
    seen = []

    def during(unit):
        marvel0.sup.invalidate()
        view = marvel0.sup.status()
        seen.append((view["state"], view["can_stop"], view["operation"]["action"]))

    marvel0.on_stop = during
    stop(marvel0)
    assert seen == [("stopping", False, "stop")]


def test_a_unit_that_is_still_shutting_down_is_not_offered_a_stop(marvel0):
    running(marvel0)
    marvel0.stop_mode = "slow"
    stop(marvel0)
    marvel0.sup.invalidate()
    view = marvel0.sup.status()
    assert (view["state"], view["can_stop"]) == ("stopping", False)


def test_other_peoples_processes_are_never_read(marvel0, monkeypatch):
    running(marvel0)
    stranger = marvel0.proc / "9999" / "fd"
    stranger.mkdir(parents=True)
    (stranger / "3").symlink_to("socket:[4242]")
    touched = []

    def spy(real):
        def wrapper(path, *args, **kwargs):
            if str(path).startswith(str(marvel0.proc / "9999")):
                touched.append(str(path))
            return real(path, *args, **kwargs)

        return wrapper

    for owner, name in (
        (os, "scandir"),
        (os, "listdir"),
        (os, "readlink"),
        (os, "stat"),
        (os, "lstat"),
        (os, "open"),
        (builtins, "open"),
    ):
        monkeypatch.setattr(owner, name, spy(getattr(owner, name)))
    marvel0.sup.status()
    marvel0.sup.invalidate()
    marvel0.listen(inode=7777)
    marvel0.sup.status()
    marvel0.sockets = [s for s in marvel0.sockets if s["inode"] != 7777]
    marvel0.write_net()
    marvel0.sup.invalidate()
    stop(marvel0)
    assert touched == []


# --------------------------------------------------------------- state


def test_an_idle_machine_can_deploy(marvel0):
    view = marvel0.sup.status()
    assert view["schema"] == "levi.marvel0.deploy.v1"
    assert (view["state"], view["reason"], view["deploy"], view["failure"]) == (
        "idle",
        None,
        None,
        None,
    )
    assert view["can_deploy"] is True and view["can_stop"] is False
    assert [c["code"] for c in view["checks"]] == [
        "state_dir",
        "net_table",
        "port_free",
        "no_client",
        "policy_units",
        "arm_lock",
        "gpu_lock",
        "gpu_readable",
        "vram",
    ]
    assert {c["level"] for c in view["checks"]} == {"ok"}
    assert view["listener"] == {
        "port": 8000,
        "present": False,
        "ours": None,
        "established": 0,
    }
    assert view["unit"] == {"state": "absent", "sub": None}
    assert view["live_annotation"] is None and view["operation"] is None
    assert view["gpu"] == {
        "state": "ok",
        "total_mib": TOTAL_MIB,
        "used_mib": 9100,
        "free_mib": TOTAL_MIB - 9100,
        "server_holds_mib": None,
        "server_holds_basis": "reserved_fraction",
    }
    assert view["vram"]["margin_mib"] == 1024
    assert view["vram"]["presets"] == [
        {"fraction": 0.22, "required_mib": 8198, "enough": True},
        {"fraction": 0.3, "required_mib": 10807, "enough": True},
        {"fraction": 0.45, "required_mib": 15698, "enough": True},
    ]
    assert view["vram"]["hint"] == (
        "Fractions 0.30 and 0.45 need the live vLLM asleep or stopped"
    )
    assert view["confirm"] == {
        "deploy": "deploy {model_id}",
        "stop": "stop policy server",
        "client_override": "stop with client connected",
    }
    assert view["notes"][0]["code"] == "source_hash_unmanifested"
    assert view["estop"]["always"] is True and view["estop"]["keep_hand"] is False
    assert "e-stop" in view["estop"]["text"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:]+[+-]\d\d:\d\d", view["sampled_at"])
    assert_no_paths(marvel0, view)


def test_a_deployed_server_goes_from_starting_to_ready(marvel0):
    running(marvel0, ready=False)
    view = marvel0.sup.status()
    assert view["state"] == "starting"
    assert view["unit"]["state"] == "activating" and view["can_stop"] is True
    assert view["can_deploy"] is False
    assert view["deploy"]["phase"] == "started"
    assert view["readiness"]["listening"] is False
    marvel0.log(*marvel0.ready_lines()[:-1])
    marvel0.units[record.unit_name(SFT)]["active"] = "active"
    marvel0.clock.advance(3)
    marvel0.sup.invalidate()
    view = marvel0.sup.status()
    assert view["state"] == "starting"
    assert view["readiness"] == {
        "norm_stats": True,
        "rtc_enabled": True,
        "rtc_warmup": True,
        "cfg": None,
        "listening": False,
    }
    marvel0.server_comes_up()
    marvel0.log(LISTEN)  # (server_comes_up wrote it; a repeat changes nothing)
    marvel0.clock.advance(3)
    marvel0.sup.invalidate()
    view = marvel0.sup.status()
    assert view["state"] == "ready" and view["reason"] is None
    assert view["listener"]["present"] and view["listener"]["ours"] is True
    assert view["deploy"]["ready_at"]
    assert view["can_stop"] is True and view["can_deploy"] is False
    assert_no_paths(marvel0, view)


def test_a_plain_server_is_ready_without_rtc_lines(marvel0):
    running(marvel0, rtc=False)
    view = marvel0.sup.status()
    assert view["state"] == "ready"
    assert view["readiness"]["rtc_enabled"] is None


def test_a_cfg_server_needs_its_guidance_line(marvel0):
    marvel0.deploy(model=CFG, accept_unverified=True, rtc=False)
    marvel0.server_comes_up(model=CFG, rtc=False, cfg=True)
    marvel0.sup.invalidate()
    view = marvel0.sup.status()
    assert (view["state"], view["readiness"]["cfg"]) == ("ready", True)


def test_a_server_that_never_logs_rtc_is_a_failed_deploy(marvel0):
    running(marvel0, ready=False)
    unit = record.unit_name(SFT)
    marvel0.units[unit]["active"] = "active"
    marvel0.log(marvel0.norm_line(), LISTEN)  # no RTC lines
    marvel0.sockets.append(
        {"port": 8000, "state": "0A", "inode": 4242, "proto": "tcp", "remote": 0}
    )
    marvel0.write_net()
    marvel0.own(unit, 777, [4242])
    assert marvel0.sup.status()["state"] == "starting", (
        "just listening: give it a moment"
    )
    marvel0.clock.advance(10)
    marvel0.sup.invalidate()
    view = marvel0.sup.status()
    assert view["state"] == "failed" and view["failure"]["kind"] == "rtc_not_ready"
    assert "RTC" in view["failure"]["text"] and view["can_stop"] is True


def test_a_benign_allocator_warning_does_not_fail_a_serving_server(marvel0):
    # With the tight fraction XLA logs "ran out of memory trying to allocate ...
    # this is not a failure" and carries on: the server is ready, not failed.
    running(marvel0, ready=False)
    marvel0.log(BFC_WARNING, BFC_NOTE)
    marvel0.server_comes_up()
    marvel0.log(BFC_WARNING, BFC_NOTE)
    marvel0.clock.advance(10)
    marvel0.sup.invalidate()
    view = marvel0.sup.status()
    assert (view["state"], view["failure"]) == ("ready", None)
    assert view["can_stop"] is True and view["can_deploy"] is False
    assert marvel0.sup.log(20)["classification"]["failure"] is None


def test_a_request_time_oom_on_a_ready_server_does_not_fail_it(marvel0):
    running(marvel0)
    marvel0.log(OOM)  # one request ran out of memory; the server keeps serving
    marvel0.clock.advance(10)
    marvel0.sup.invalidate()
    assert marvel0.sup.status()["state"] == "ready"


@pytest.mark.parametrize(
    "lines, kind",
    [([OOM], "xla_oom"), (ARGPARSE, "argparse"), ([IN_USE], "address_in_use")],
)
def test_a_crashed_start_is_failed_with_its_reason(marvel0, lines, kind):
    running(marvel0, ready=False)
    marvel0.log(*lines)
    marvel0.units.clear()
    view = marvel0.sup.status()
    assert (view["state"], view["failure"]["kind"]) == ("failed", kind)
    assert view["can_stop"] is False and view["can_deploy"] is True
    assert view["live_annotation"] is None, "the unit is gone: the memory is free"
    assert_no_paths(marvel0, view)


def test_a_server_that_exits_after_it_was_ready_is_dead(marvel0):
    running(marvel0)
    marvel0.units.clear()
    marvel0.sockets.clear()
    marvel0.write_net()
    marvel0.sup.invalidate()
    view = marvel0.sup.status()
    assert (view["state"], view["reason"]["code"]) == ("dead", "exited_after_ready")
    assert view["can_deploy"] is True


def test_a_failed_or_dead_record_never_blocks_a_new_deploy(marvel0):
    running(marvel0)
    marvel0.units.clear()
    marvel0.sockets.clear()
    marvel0.write_net()
    assert marvel0.sup.status()["state"] == "dead"
    answer = marvel0.deploy(request_id="req-000002-bbbb")
    assert answer["state"] == "starting"


def test_a_server_started_by_hand_is_foreign_and_untouchable(marvel0):
    marvel0.listen()
    view = marvel0.sup.status()
    assert (view["state"], view["reason"]["code"]) == (
        "foreign_listener",
        "hand_started",
    )
    assert "Ctrl+C" in view["reason"]["text"] and "8000" in view["reason"]["text"]
    assert view["can_stop"] is False and view["can_deploy"] is False
    assert "foreign_listener" in view["stop_blockers"]
    assert view["live_annotation"]["text"] == (
        "server holds ? MiB, live annotation cannot start"
    )
    assert view["listener"]["ours"] is False


def test_a_listener_next_to_our_running_unit_is_not_ours(marvel0):
    running(marvel0)
    marvel0.sockets = [s for s in marvel0.sockets if s["state"] != "0A"]
    marvel0.listen(inode=555)
    marvel0.sup.invalidate()
    view = marvel0.sup.status()
    assert (view["state"], view["reason"]["code"]) == (
        "foreign_listener",
        "listener_not_ours",
    )


def test_a_unit_whose_processes_cannot_be_read_does_not_own_the_port(marvel0):
    running(marvel0)
    marvel0.fail.add("show-cgroup")
    marvel0.sup.invalidate()
    view = marvel0.sup.status()
    assert (view["state"], view["reason"]["code"]) == (
        "foreign_listener",
        "owner_unknown",
    )
    assert view["listener"]["ours"] is None


def test_the_memory_a_server_holds_is_shown_while_it_runs(marvel0):
    running(marvel0)
    view = marvel0.sup.status()
    assert view["gpu"]["server_holds_mib"] == 7174
    assert view["live_annotation"] == {
        "blocked": True,
        "text": "server holds 7174 MiB, live annotation cannot start",
    }
    stop(marvel0)
    assert marvel0.sup.status()["live_annotation"] is None


def test_an_outgoing_connection_to_port_8000_elsewhere_does_not_block_the_stop(marvel0):
    running(marvel0)
    marvel0.connect_out("203.0.113.7")
    marvel0.sup.invalidate()
    view = marvel0.sup.status()
    assert view["listener"]["established"] == 0
    assert "client_connected" not in view["stop_blockers"]
    stop(marvel0)
    assert marvel0.stops()


def test_a_connected_client_is_a_stop_blocker(marvel0):
    running(marvel0)
    marvel0.connect()
    marvel0.sup.invalidate()
    view = marvel0.sup.status()
    assert view["listener"]["established"] == 2
    assert view["stop_blockers"] == ["client_connected"]
    client = next(c for c in view["checks"] if c["code"] == "no_client")
    assert client["level"] == "refuse"


def test_another_policy_unit_makes_a_deploy_impossible_but_not_the_state(marvel0):
    marvel0.foreign_units = ["levi-aeri-policy-forward"]
    view = marvel0.sup.status()
    assert view["state"] == "idle" and view["can_deploy"] is False
    unit = next(c for c in view["checks"] if c["code"] == "policy_units")
    assert unit["level"] == "refuse" and unit["params"]["units"] == [
        "levi-aeri-policy-forward.service"
    ]


@pytest.mark.parametrize(
    "setup, check, level",
    [
        (lambda r: r.hold(r.site.arm_lock_file, 5001), "arm_lock", "refuse"),
        (lambda r: r.hold(r.site.gpu_lock_file, 6001), "gpu_lock", "refuse"),
        (lambda r: setattr(r, "gpu", [TOTAL_MIB - 8000, TOTAL_MIB]), "vram", "refuse"),
        (lambda r: r.fail.add("nvidia-smi"), "gpu_readable", "unknown"),
        (lambda r: r.fail.add("list-units"), "policy_units", "unknown"),
        (lambda r: r.connect(), "no_client", "refuse"),
    ],
)
def test_each_check_shows_its_own_finding(marvel0, setup, check, level):
    setup(marvel0)
    view = marvel0.sup.status()
    rows = {c["code"]: c for c in view["checks"]}
    assert rows[check]["level"] == level, rows[check]
    assert rows[check]["text"]
    assert view["can_deploy"] is False
    # An unreadable GPU also leaves the memory check unanswered; nothing else
    # is touched by the one broken thing.
    expected = {check} | ({"vram"} if check == "gpu_readable" else set())
    assert {c for c, r in rows.items() if r["level"] != "ok"} == expected


def test_the_state_poll_does_not_read_the_run_registry(marvel0):
    # The page polls every few seconds: the registry (its key, every run's
    # journal, the recorded runner's process entry) is read when Deploy is
    # pressed, not on a poll. The arm lock row is what a poll shows.
    calls = []
    real = marvel0.host.aeri_runs

    def counted():
        calls.append(1)
        return real()

    marvel0.host.aeri_runs = counted
    marvel0.aeri.append({"run_id": "r1", "backend": "standalone", "runner_alive": True})
    view = marvel0.sup.status()
    assert calls == []
    assert "aeri_run" not in {c["code"] for c in view["checks"]}
    status, code, detail = marvel0.refused(marvel0.deploy)
    assert (status, code, detail["run_id"]) == (409, "aeri_run_live", "r1")
    assert calls, "the deploy reads it"


@pytest.mark.parametrize(
    "broken",
    ["show-state", "list-units", "net-tcp", "record", "nvidia-smi", "show-cgroup"],
)
def test_an_unreadable_source_never_raises_and_never_means_idle(marvel0, broken):
    running(marvel0)
    if broken == "record":
        (marvel0.state / "record.json").write_text("{broken")
    else:
        marvel0.fail.add(broken)
        marvel0.write_net()
    marvel0.sup.invalidate()
    view = marvel0.sup.status()  # does not raise
    if broken == "nvidia-smi":
        assert view["state"] == "ready" and view["gpu"]["state"] == "unknown"
        return
    assert view["state"] in ("unknown", "foreign_listener"), view["state"]
    assert view["state"] != "idle"
    assert view["can_deploy"] is False
    if view["state"] == "unknown":
        assert view["reason"]["code"] in (
            "net_unreadable",
            "systemd_unreadable",
            "record_unreadable",
        )
    assert_no_paths(marvel0, view)


def test_a_corrupt_record_is_unknown_and_a_new_deploy_is_still_checked(marvel0):
    directory = record.state_dir((), marvel0.workspace)
    (directory / "record.json").write_text("{broken")
    view = marvel0.sup.status()
    assert (view["state"], view["reason"]["code"]) == ("unknown", "record_unreadable")
    assert view["deploy"] is None and view["can_deploy"] is False
    answer = marvel0.deploy()  # the deploy overwrites it, after the same checks
    assert answer["state"] == "starting"


def test_the_state_is_read_at_most_every_two_seconds(marvel0):
    marvel0.sup.status()
    count = len(marvel0.calls)
    marvel0.clock.advance(1.5)
    marvel0.sup.status()
    assert len(marvel0.calls) == count
    marvel0.clock.advance(1.0)
    marvel0.sup.status()
    assert len(marvel0.calls) > count


def test_a_deploy_and_a_stop_end_the_cache(marvel0):
    assert marvel0.sup.status()["state"] == "idle"
    marvel0.deploy()
    assert marvel0.sup.status()["state"] == "starting"
    marvel0.server_comes_up()
    marvel0.sup.invalidate()
    assert marvel0.sup.status()["state"] == "ready"
    stop(marvel0)
    assert marvel0.sup.status()["state"] == "idle"


@pytest.mark.parametrize(
    "state_setup, marker, session, keep",
    [
        ("ready", "present", True, False),  # a healthy server: nothing to warn of
        ("dead", "present", False, True),
        ("dead", "absent", False, False),
        ("dead", "absent", True, True),
        ("dead", "absent", None, True),
        ("dead", "unknown", False, True),
        ("failed", "absent", False, False),
        ("failed", "present", False, True),
        ("foreign", "present", False, True),
        ("idle", "present", True, False),
    ],
)
def test_the_hand_on_the_e_stop_notice(marvel0, state_setup, marker, session, keep):
    marvel0.robot = {"marker": marker, "open_session": session}
    if state_setup == "ready":
        running(marvel0)
    elif state_setup == "dead":
        running(marvel0)
        marvel0.units.clear()
        marvel0.sockets.clear()
        marvel0.write_net()
    elif state_setup == "failed":
        running(marvel0, ready=False)
        marvel0.units.clear()
        marvel0.log(OOM)
    elif state_setup == "foreign":
        marvel0.listen()
    marvel0.sup.invalidate()
    view = marvel0.sup.status()
    assert view["estop"]["keep_hand"] is keep, view["state"]
    assert view["estop"]["always"] is True


def test_an_unreadable_robot_context_keeps_the_notice_on(marvel0):
    running(marvel0)
    marvel0.units.clear()
    marvel0.sockets.clear()
    marvel0.write_net()

    def broken():
        raise OSError("no")

    marvel0.host.robot_context = broken
    marvel0.sup.invalidate()
    assert marvel0.sup.status()["estop"]["keep_hand"] is True


# --------------------------------------------------------------- the log


def test_without_a_deploy_there_is_no_log(marvel0):
    log = marvel0.sup.log(50)
    assert log["schema"] == "levi.marvel0.deploy_log.v1"
    assert (log["log_id"], log["lines"], log["size_bytes"]) == (None, [], 0)
    assert set(log["classification"]) == {
        "norm_stats",
        "rtc_enabled",
        "rtc_warmup",
        "cfg",
        "listening",
        "failure",
    }


def test_the_log_is_the_printable_tail_without_paths(marvel0):
    running(marvel0)
    marvel0.log(
        "\x1b[1mbold\x1b[0m line", "x" * 400, "/tmp/some/where/file.txt is here"
    )
    log = marvel0.sup.log(500)
    assert re.fullmatch(
        rf"{record.unit_name(SFT)}-\d{{8}}T\d{{6}}Z\.log", log["log_id"]
    ), "a name only, never a path"
    assert log["size_bytes"] > 0
    assert log["lines"][-3] == "bold line"
    assert len(log["lines"][-2]) == 300
    assert log["lines"][-1] == "file.txt is here"
    assert log["classification"] == {
        "norm_stats": True,
        "rtc_enabled": True,
        "rtc_warmup": True,
        "cfg": None,
        "listening": True,
        "failure": None,
    }
    assert_no_paths(marvel0, log)


def test_the_log_line_count_is_clamped(marvel0):
    running(marvel0)
    marvel0.log(*[f"line {n}" for n in range(300)])
    assert len(marvel0.sup.log(1000)["lines"]) == 200
    assert len(marvel0.sup.log(0)["lines"]) == 1
    assert len(marvel0.sup.log(5)["lines"]) == 5


def test_a_failure_shows_in_the_log_classification(marvel0):
    running(marvel0, ready=False)
    marvel0.log(OOM)
    assert marvel0.sup.log(10)["classification"]["failure"] == "xla_oom"


def test_the_log_of_a_record_with_a_forged_file_name_is_not_read(marvel0):
    running(marvel0)
    record.update_record(marvel0.state, log_file="../../../etc/passwd")
    log = marvel0.sup.log(10)
    assert (log["log_id"], log["lines"]) == (None, [])


# --------------------------------------------------------------- everything together


def test_no_response_of_a_whole_session_holds_a_path(marvel0):
    out = [marvel0.sup.status()]
    out.append(running(marvel0))
    out.append(marvel0.sup.status())
    out.append(marvel0.sup.log(50))
    out.append(stop(marvel0))
    out.append(marvel0.sup.status())
    for value in out:
        assert_no_paths(marvel0, value)
    marvel0.listen()
    marvel0.sockets = [s for s in marvel0.sockets if s["state"] != "0A"] + [
        {"port": 8000, "state": "0A", "inode": 5, "proto": "tcp", "remote": 0}
    ]
    for call, args in (
        (marvel0.sup.deploy, (body(request_id="req-000009-zzzz"),)),
        (marvel0.sup.stop, ({**STOP, "request_id": "req-stop-0009"},)),
    ):
        with pytest.raises(HTTPException) as caught:
            call(*args)
        assert_no_paths(marvel0, caught.value.detail)


def test_every_command_of_a_session_is_systemd_or_nvidia_smi(marvel0):
    running(marvel0)
    marvel0.sup.status()
    stop(marvel0)
    for argv, _ in marvel0.calls:
        assert argv[0] in deploy.ALLOWED_COMMANDS
    verbs = {tuple(argv[:3]) for argv, _ in marvel0.calls if argv[0] == "systemctl"}
    assert verbs <= {
        ("systemctl", "--user", "show"),
        ("systemctl", "--user", "list-units"),
        ("systemctl", "--user", "show-environment"),
        ("systemctl", "--user", "stop"),
    }


# --------------------------------------------------------------- hardening


def test_a_lock_left_by_an_earlier_deploy_does_not_block_the_next(marvel0):
    marvel0.deploy()
    first = marvel0.locks_made[0]
    marvel0.units.clear()  # it failed at once; the watcher has not looked yet
    marvel0.log(*ARGPARSE)
    assert first.held
    marvel0.deploy(request_id="req-000002-bbbb")
    assert not first.held and marvel0.locks_made[1].held


def test_an_unexpected_error_in_the_final_checks_frees_the_lock(marvel0, monkeypatch):
    real_read = deploy.net.read
    calls = []

    def read(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:  # the first read is the preflight's, the next is final
            raise RuntimeError("boom")
        return real_read(*args, **kwargs)

    monkeypatch.setattr(deploy.net, "read", read)
    with pytest.raises(RuntimeError):
        marvel0.sup.deploy(body())
    assert not [lock for lock in marvel0.locks_made if lock.held]
    assert (
        marvel0.record()["phase"] == "aborted"
        and marvel0.record()["abort_code"] == "error"
    )
    assert marvel0.systemd_runs() == []


def test_a_gpu_lock_that_cannot_be_opened_is_not_ours_to_take(marvel0):
    def denied(path):
        raise OSError("permission denied")

    marvel0.host.gpu_lock = denied
    refuse(marvel0, 409, "gpu_lock_foreign", intent=True)


@pytest.mark.parametrize("fraction", [None, [], "abc", float("nan"), float("inf")])
def test_a_fraction_that_is_not_a_number_is_not_a_preset(marvel0, fraction):
    assert marvel0.refused(marvel0.sup.deploy, body(fraction=fraction))[:2] == (
        422,
        "fraction_not_allowed",
    )
    assert marvel0.calls == []


def test_a_guidance_scale_that_is_not_a_number_is_out_of_range(marvel0):
    status, code, _ = marvel0.refused(
        marvel0.sup.deploy,
        body(model=CFG, accept_unverified=True, cfg_guidance_scale="x"),
    )
    assert (status, code) == (422, "cfg_scale_out_of_range")


def test_status_never_raises_even_when_a_read_blows_up(marvel0):
    running(marvel0)

    def broken(argv, timeout_s):
        raise RuntimeError("boom")

    marvel0.host.run = broken
    marvel0.sup.invalidate()
    view = marvel0.sup.status()
    assert view["state"] == "unknown"
    assert view["reason"]["code"] == "unclassified"
    assert (view["can_deploy"], view["can_stop"], view["checks"]) == (False, False, [])
    assert view["estop"] == {
        "always": True,
        "keep_hand": True,
        "text": deploy.ESTOP_TEXT,
    }
    assert_no_paths(marvel0, view)


def test_a_state_folder_that_cannot_be_used_is_unknown_not_idle(marvel0):
    marvel0.workspace = Workspace.at(Path(marvel0.site.rollout_root) / "ws")
    marvel0.sup.workspace = marvel0.workspace
    view = marvel0.sup.status()
    assert (view["state"], view["reason"]["code"]) == ("unknown", "record_unreadable")
    row = next(c for c in view["checks"] if c["code"] == "state_dir")
    assert row["level"] == "refuse" and view["can_deploy"] is False
    assert not Path(marvel0.site.rollout_root).exists(), "nothing was written there"


def test_the_real_catalog_fits_the_supervisor(marvel0):
    """The supervisor takes its models from ``levi.marvel0.models`` unless told
    otherwise: the attributes it reads exist there."""
    from levi.marvel0 import models

    models.cache_clear()
    sup = deploy.Supervisor(
        marvel0.host, site_loader=lambda: marvel0.site, workspace=marvel0.workspace
    )
    answer = sup.deploy(body())
    assert answer["deploy"]["config"] == "pi05_fr3_all_state"
    marvel0.units.clear()
    marvel0.sockets.clear()
    marvel0.write_net()
    refused = marvel0.refused(sup.deploy, body(model=CFG, request_id="req-000002-bbbb"))
    assert refused[:2] == (409, "unverified_not_accepted"), (
        "recap models are unverified"
    )
    models.cache_clear()


def test_the_real_gpu_lock_is_held_until_the_server_is_resident(marvel0):
    from levi.live import gpumgr

    marvel0.host.gpu_lock = deploy.Host.gpu_lock  # the production factory
    marvel0.deploy()
    other = gpumgr.GpuLock(marvel0.site.gpu_lock_file, "other", allow_unavailable=False)
    assert other.acquire() is False, "held by the deploy"
    marvel0.server_comes_up()
    marvel0.clock.advance(3)
    assert marvel0.sup.watch_once() is False
    assert other.acquire() is True, "let go once the server is resident"
    other.release()


def test_the_watcher_thread_lets_go_of_the_lock_on_its_own(marvel0):
    marvel0.host.background = True
    brought = []

    def up(seconds):
        if not brought:
            brought.append(1)
            marvel0.server_comes_up()

    marvel0.clock.on_sleep = up  # the server comes up during the watcher's first wait
    marvel0.deploy()
    watcher = marvel0.sup._watcher
    assert watcher.name == "levi-marvel0-deploy-watch" and watcher.daemon
    watcher.join(10)
    assert not watcher.is_alive()
    assert not marvel0.locks_made[0].held
    assert marvel0.record()["ready_at"] and marvel0.record()["lock_released_at"]
    assert marvel0.clock.sleeps and marvel0.clock.sleeps[0] == deploy.WATCH_EVERY_S


def test_a_watcher_that_is_leaving_does_not_strand_a_hold_that_lands_now(marvel0):
    # The old watcher has seen no holder and is about to return when the next
    # deploy takes the lock: it must look again instead of leaving it unwatched.
    sup = marvel0.sup
    calls = []

    def watch_once():
        calls.append(1)
        if len(calls) == 1:
            sup._hold(
                marvel0.site, marvel0.state, marvel0.make_lock("x"), "req-000009-zzzz"
            )
        else:
            with sup._mutex:
                sup._holder = None
        return False

    sup.watch_once = watch_once
    with sup._mutex:
        sup._watcher = threading.current_thread()
    sup._watch_loop()
    assert len(calls) == 2, "the hold that landed was watched"
    assert sup._holder is None and sup._watcher is None


def test_a_new_hold_after_the_watcher_has_gone_starts_a_new_one(marvel0):
    marvel0.host.background = True
    marvel0.host.sleep = lambda seconds: None
    sup = marvel0.sup

    def gone():  # the lock is let go: the watcher has nothing more to do
        with sup._mutex:
            sup._holder = None
        return False

    sup.watch_once = gone
    sup._hold(marvel0.site, marvel0.state, marvel0.make_lock("x"), "req-000009-zzzz")
    first = sup._watcher
    first.join(10)
    assert not first.is_alive() and sup._watcher is None
    sup._hold(marvel0.site, marvel0.state, marvel0.make_lock("y"), "req-000010-zzzz")
    second = sup._watcher
    assert second is not first and second is not None
    second.join(10)


# --------------------------------------------------------------- what the code may not do

MODULES = ("deploy.py", "net.py", "record.py", "deploy_api.py")
FORBIDDEN_CODE = [
    r"\bos\.kill",
    r"killpg",
    r"\bpkill\b",
    r"\bpgrep\b",
    r"\bsignal\.",
    r"\bPopen\b",
    r"\bsocket\.",
    r"create_connection",
    r"\bsudo\b",
    r"\bssh\b",
    r"\bcurl\b",
    r"\bros2\b",
    r"\bos\.system",
    r"shell\s*=\s*True",
    r"/home/",
    r"wenkai",
]


def code_of(name):
    """The dotted names and the string values of a module's code: its
    docstrings and comments, which say what is never done, are left out."""
    tree = ast.parse((Path(deploy.__file__).parent / name).read_text())
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docs.add(id(first.value))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docs:
                found.append(node.value)
        elif isinstance(node, (ast.Attribute, ast.Name)):
            found.append(ast.unparse(node))
    return found


@pytest.mark.parametrize("name", MODULES)
def test_the_modules_name_no_signal_socket_shell_or_machine_path(name):
    text = "\n".join(code_of(name))
    for pattern in FORBIDDEN_CODE:
        assert not re.search(pattern, text), (name, pattern)


@pytest.mark.parametrize("name", MODULES)
def test_the_only_proc_files_named_are_the_socket_and_lock_tables(name):
    for value in code_of(name):
        for found in re.findall(r"/proc[^\s]*", value):
            assert found in ("/proc/net", "/proc/locks"), (name, found)


def test_subprocess_is_used_only_by_the_one_runner():
    assert code_of("deploy.py").count("subprocess.run") == 1
    for name in ("net.py", "record.py", "deploy_api.py"):
        assert not [x for x in code_of(name) if "subprocess" in x]
