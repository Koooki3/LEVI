"""The stand-in ``Specs`` of the rehearsals: the same rules, limits and stop
properties as ``stack_units.real_specs``, with the commands replaced by the
scripts of this folder. ``assert_fake`` is the guard every rehearsal runs
first: no unit may run anything but this interpreter on these scripts (or the
reviewed wrapper in front of them)."""

import subprocess
import sys
from pathlib import Path

from levi.marvel0 import stack_units

HERE = Path(__file__).resolve().parent
PYTHON = sys.executable
SCRIPTS = {
    name: HERE / f"{name}.py" for name in ("fake_a", "fake_b", "fake_c", "fake_oneshot")
}
ENV_FILE = HERE / "fake_env.sh"


def leftover_standins(root) -> list[str]:
    """The stand-in programs still running that belong to the test whose folder
    is ``root``: every stand-in's command line names a file under its test's
    folder (``--health=``, ``--alive=``, the one-shots' ``owner=``). A check of
    every stand-in on the machine blamed a test for the stand-ins of another
    test file running at the same time (pytest -n, 2026-10-27)."""
    listed = subprocess.run(
        ["pgrep", "-af", str(HERE / "fake_")],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    mine = f"{Path(root)}/"
    return [line for line in listed.splitlines() if mine in line]


def assert_fake(argv, *, wrapper=None) -> None:
    """Raise unless ``argv`` runs this interpreter on one of the stand-in
    scripts, optionally behind the reviewed wrapper and the fake environment."""
    tokens = list(argv)
    if wrapper is not None and tokens[:1] == ["/bin/bash"]:
        if tokens[1:4] != [str(wrapper), str(ENV_FILE), "--"]:
            raise AssertionError(
                f"not the wrapper on the fake environment: {tokens[:4]}"
            )
        tokens = tokens[4:]
    if tokens[:1] != [PYTHON] or Path(tokens[1]) not in SCRIPTS.values():
        raise AssertionError(f"not a stand-in program: {tokens[:2]}")


def fake_specs(
    health,
    *,
    port=5056,
    a="ok",
    b="ok",
    c="ok",
    gripper="ok",
    controllers="ok",
    ee="ok",
    via_wrapper=False,
    ready_timeout_s=10.0,
    stop_timeout_s=4,
    marker_wait_s=0.3,
    names=None,
    qos_file=None,
    alive_file=None,
    nodes="ok",
    nodes_delay_s=0.0,
    controllers_delay_s=0.0,
    ee_delay_s=0.0,
) -> stack_units.Specs:
    """``names``: unit names by key (``{"a": "levi-aeri-stack-marvel0-rehearsal-a"}``)."""
    if port in (5000, 5001, 5100, 7470, 8000):
        raise AssertionError("a protected port")
    names = names or {k: stack_units.UNIT_PREFIX + k for k in "abc"}

    def behind(command):
        if via_wrapper:
            return (
                "/bin/bash",
                str(stack_units.WRAPPER),
                str(ENV_FILE),
                "--",
                *command,
            )
        return tuple(command)

    py = PYTHON
    commands = {
        "a": [py, str(SCRIPTS["fake_a"]), f"--health={health}", f"--mode={a}"],
        "b": [py, str(SCRIPTS["fake_b"]), f"--mode={b}", f"--alive={alive_file}"],
        "c": [
            py,
            str(SCRIPTS["fake_c"]),
            f"--flask_port={port}",
            f"--health={health}",
            f"--mode={c}",
        ],
    }
    real = {
        "a": (
            stack_units.A_READY,
            stack_units.A_FAIL,
            "hardware_active",
            None,
            ("LimitRTPRIO=99", "LimitMEMLOCK=infinity"),
        ),
        "b": (stack_units.B_READY, stack_units.B_FAIL, None, None, ("LimitRTPRIO=99",)),
        "c": (
            stack_units.c_ready(port),
            stack_units.C_FAIL,
            "controller_active",
            port,
            (),
        ),
    }
    units = {}
    for key in "abc":
        ready, fail, flag, listen, limits = real[key]
        units[key] = stack_units.UnitSpec(
            key,
            names[key],
            f"stand-in for terminal {key.upper()}",
            stack_units.check_argv(behind(commands[key])),
            None,
            ready,
            fail,
            flag,
            listen,
            limits,
            ready_timeout_s=ready_timeout_s,
            stop_timeout_s=stop_timeout_s,
        )
        assert_fake(
            units[key].argv, wrapper=stack_units.WRAPPER if via_wrapper else None
        )

    def one_shot(*args, delay=0.0):
        # owner= names the test's folder (the health file lives there), so a
        # leaked one-shot is found by leftover_standins like the long-lived ones.
        extra = ([f"delay={delay}"] if delay else []) + [f"owner={health}"]
        return stack_units.check_argv(
            behind([py, str(SCRIPTS["fake_oneshot"]), *args, *extra]), long_lived=False
        )

    return stack_units.Specs(
        profile="fake",
        units=units,
        wrapper=str(stack_units.WRAPPER),
        env_file=str(ENV_FILE),
        robot_ip="10.42.0.157",
        server_port=port,
        marker_wait_s=marker_wait_s,
        franka_control=str(HERE),
        com_port=str(Path(__file__)),  # (only its existence is asked)
        qos_file=str(qos_file) if qos_file else None,
        gripper_cmd=lambda position: one_shot("gripper", str(float(position)), gripper),
        controllers_cmd=one_shot(
            "controllers", controllers, str(alive_file), delay=controllers_delay_s
        ),
        ee_cmd=one_shot("ee", ee, delay=ee_delay_s),
        nodes_cmd=one_shot("nodes", nodes, str(alive_file), delay=nodes_delay_s),
    )
