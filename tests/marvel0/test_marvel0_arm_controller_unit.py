"""The arm controller choice at terminal A (``levi/marvel0/stack_units.py``,
``stack_act.py``): with ``lab`` everything stays as it was, byte for byte; with
``anyspeedup`` only the environment file argument and the command of A differ
(same wrapper), its launch lines are read, and A's start is refused when the
checkout or its build is not there. Nothing here starts a process or reads the
real machine; the anyspeedup checkout is read (read-only) only when it is beside
this one, to keep the lines this page relies on honest."""

import re

import pytest
from arm_controller_helpers import (
    ANY_ENV,
    ANY_ROOT,
    LAB_A,
    LAB_ENV,
    anyspeedup,
    anyspeedup_checkout,
    control,
)
from stack_act_machine import ActMachine, ready_lines
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard
from test_marvel0_stack_act_units import HEALTH_OK

from levi.marvel0 import record, stack, stack_act, stack_units

# --- terminal A's unit ----------------------------------------------------------------------


def test_the_lab_controller_is_the_command_and_environment_of_today():
    specs = stack_units.real_specs(control())
    a = specs.units["a"]
    assert a.argv == (
        "/bin/bash",
        str(stack_units.WRAPPER),
        LAB_ENV,
        "--",
        *LAB_A,
    )
    assert a.title == "FR3 arm ROS 2 stack (terminal A)"
    assert a.ready is stack_units.A_READY and a.fail is stack_units.A_FAIL
    assert (a.cwd, a.health_flag, a.listen_port) == (None, "hardware_active", None)
    assert a.limits == ("LimitRTPRIO=99", "LimitMEMLOCK=infinity")
    assert specs.env_file == LAB_ENV
    assert (specs.arm_controller, specs.arm_preset, specs.arm_checks) == (
        "lab",
        None,
        (),
    )
    assert specs.arm_identity == ("lab", None, None)
    assert a.setenv == () and specs.arm_env is None


def test_the_lab_units_do_not_depend_on_the_inert_anyspeedup_values():
    plain = stack_units.real_specs(control())
    inert = stack_units.real_specs(
        control(arm_controller="lab", arm_preset="S1", anyspeedup_root=ANY_ROOT)
    )
    assert plain.units == inert.units
    assert plain.env_file == inert.env_file
    for name in ("controllers_cmd", "ee_cmd", "nodes_cmd"):
        assert getattr(plain, name) == getattr(inert, name)
    assert plain.gripper_cmd(0.0) == inert.gripper_cmd(0.0)


@pytest.mark.parametrize("preset", ["today", "S1"])
def test_the_anyspeedup_controller_changes_only_the_environment_file_and_the_command(
    preset,
):
    specs = stack_units.real_specs(anyspeedup(preset))
    lab = stack_units.real_specs(control())
    a = specs.units["a"]
    assert a.argv == (
        "/bin/bash",
        str(stack_units.WRAPPER),
        ANY_ENV,
        "--",
        "ros2",
        "launch",
        "--noninteractive",
        "anyspeedup_franka_controllers",
        "impedance.launch.py",
        "robot_type:=fr3",
        "robot_ip:=10.42.0.157",
        "load_gripper:=false",
        f"preset:={preset}",
    )
    assert a.argv[1] == str(stack_units.WRAPPER)
    lab_a = lab.units["a"]
    assert (a.cwd, a.health_flag, a.listen_port, a.limits) == (
        lab_a.cwd,
        lab_a.health_flag,
        lab_a.listen_port,
        lab_a.limits,
    )
    assert (a.ready_timeout_s, a.stop_timeout_s) == (
        lab_a.ready_timeout_s,
        lab_a.stop_timeout_s,
    )
    assert preset in a.title and "anyspeedup" in a.title
    # B, C and every one-shot check keep the rig's own environment file
    assert specs.env_file == LAB_ENV
    assert specs.units["b"] == lab.units["b"] and specs.units["c"] == lab.units["c"]
    for name in ("controllers_cmd", "ee_cmd", "nodes_cmd"):
        assert getattr(specs, name) == getattr(lab, name)
        assert getattr(specs, name)[2] == LAB_ENV
    assert (specs.arm_controller, specs.arm_preset) == ("anyspeedup", preset)
    setup = f"{ANY_ROOT}/controllers/fr3_serl/ws/install/setup.bash"
    package = (
        f"{ANY_ROOT}/controllers/fr3_serl/ws/install/anyspeedup_franka_controllers"
    )
    assert specs.arm_checks == (
        ("arm_controller_missing", "exists", (ANY_ENV,)),
        ("arm_controller_unsafe", "safe_file", (ANY_ENV,)),
        # what env.sh sources (setup.bash) and the package: a partial build has one
        ("arm_controller_not_built", "exists", (setup, package)),
    )
    assert specs.arm_identity == ("anyspeedup", preset, ANY_ENV)
    # env.sh sources the rig's fr3_env.sh of the control file, not a guess
    assert a.setenv == (f"ANYSPEEDUP_FR3_ENV={LAB_ENV}",)
    assert all(not u.setenv for k, u in specs.units.items() if k != "a")


def test_the_unit_properties_are_the_same_for_both_controllers():
    lab = stack_units.systemd_run_argv(
        stack_units.real_specs(control()).units["a"], "/state/a.log", ("ROS_DISTRO",)
    )
    any_ = stack_units.systemd_run_argv(
        stack_units.real_specs(anyspeedup("S1")).units["a"],
        "/state/a.log",
        ("ROS_DISTRO",),
    )
    properties = [t for t in lab if t.startswith("--property=")]
    assert properties == [t for t in any_ if t.startswith("--property=")]
    # the one difference in the environment: A is told where the rig's file is
    assert [t for t in any_ if t.startswith("--setenv=")] == [
        "--setenv=PYTHONUNBUFFERED=1",
        "--setenv=RCUTILS_COLORIZED_OUTPUT=0",
        f"--setenv=ANYSPEEDUP_FR3_ENV={LAB_ENV}",
    ]
    assert [t for t in lab if t.startswith("--setenv=")] == [
        "--setenv=PYTHONUNBUFFERED=1",
        "--setenv=RCUTILS_COLORIZED_OUTPUT=0",
    ]
    assert [t for t in lab if t.startswith("--description=")] != [
        t for t in any_ if t.startswith("--description=")
    ]
    assert lab[:3] == any_[:3]


def test_the_wrapper_is_the_same_reviewed_file_for_both():
    assert stack_units.wrapper_sha256() == stack_units.WRAPPER_SHA256


def test_the_anyspeedup_unit_needs_the_anyspeedup_values():
    with pytest.raises(ValueError):
        stack_units.real_specs(control(arm_controller="anyspeedup"))
    with pytest.raises(ValueError):
        stack_units.real_specs(anyspeedup("S2"))
    with pytest.raises(ValueError):
        stack_units.real_specs(control(arm_controller="ros"))
    with pytest.raises(ValueError):
        stack_units.real_specs(anyspeedup(anyspeedup_root="/opt/a b"))


# --- the lines of its launch ----------------------------------------------------------------

START = "[INFO] [launch.user]: anyspeedup arm controller, preset {preset}"
DUPLICATE = (
    "[INFO] [launch.user]: A Franka arm ros2_control_node is already running (lab or "
    "anyspeedup controller). Stop it first (Ctrl+C in its terminal A)."
)


def a_lines(preset="today", start=True):
    return [*([START.format(preset=preset)] if start else []), *ready_lines("a")]


@pytest.mark.parametrize("preset", ["today", "S1"])
def test_ready_needs_the_launchs_start_line_for_the_chosen_preset(preset):
    spec = stack_units.real_specs(anyspeedup(preset)).units["a"]
    got = stack_units.read_lines(spec, a_lines(preset), HEALTH_OK, None)
    assert got["ready"] and got["failure"] is None
    assert [r["key"] for r in got["seen"]] == [
        "anyspeedup_start",
        "fifo",
        "spawners",
        "health_hardware_active",
    ]
    # no start line: the launch did not take its non-duplicate branch
    got = stack_units.read_lines(spec, a_lines(preset, start=False), HEALTH_OK, None)
    assert not got["ready"] and [r["key"] for r in got["missing"]] == [
        "anyspeedup_start"
    ]
    # another preset started (or the lab's launch, which prints no such line)
    other = "S1" if preset == "today" else "today"
    got = stack_units.read_lines(spec, a_lines(other), HEALTH_OK, None)
    assert not got["ready"]
    assert not stack_units.read_lines(
        spec, [START.format(preset=preset + "2"), *ready_lines("a")], HEALTH_OK, None
    )["ready"]


def test_the_start_line_printed_twice_is_a_second_process():
    spec = stack_units.real_specs(anyspeedup("today")).units["a"]
    lines = [*a_lines("today"), START.format(preset="today")]
    got = stack_units.read_lines(spec, lines, HEALTH_OK, None)
    assert got["duplicate"] == "anyspeedup_start" and not got["ready"]


def test_the_anyspeedup_launchs_duplicate_message_is_named():
    spec = stack_units.real_specs(anyspeedup("S1")).units["a"]
    got = stack_units.read_lines(spec, [DUPLICATE], HEALTH_OK, None)
    assert got["failure"] == "duplicate_launch" and not got["ready"]
    assert stack_units.failure_text(spec, "duplicate_launch")
    # the lab's rule cannot read it: its text is another (without "arm")
    lab = stack_units.real_specs(control()).units["a"]
    assert stack_units.read_lines(lab, [DUPLICATE], HEALTH_OK, None)["failure"] is None


def test_an_unknown_preset_is_a_failure_of_its_own():
    spec = stack_units.real_specs(anyspeedup("today")).units["a"]
    line = "preset must be one of ('today', 'S1', 'S2'), got 'x': nothing started"
    got = stack_units.read_lines(spec, [line], HEALTH_OK, None)
    assert got["failure"] == "preset_refused"
    assert stack_units.failure_text(spec, "preset_refused")


def test_the_lab_unit_does_not_read_the_anyspeedup_lines():
    spec = stack_units.real_specs(control()).units["a"]
    got = stack_units.read_lines(spec, a_lines("S1"), HEALTH_OK, None)
    assert got["ready"] and got["seen"][0]["key"] == "fifo"
    assert got["failure"] is None


def test_the_lines_are_in_the_anyspeedup_launch_when_it_is_beside_this_checkout():
    folder = anyspeedup_checkout()
    if folder is None:
        pytest.skip("no anyspeedup checkout beside this one")
    launch = folder / "anyspeedup_franka_controllers" / "launch" / "impedance.launch.py"
    lines = launch.read_text(encoding="utf-8").splitlines()
    spec = stack_units.real_specs(anyspeedup("today")).units["a"]
    wanted = {
        "anyspeedup_start": "anyspeedup arm controller, preset",
        "duplicate_launch": "A Franka arm ros2_control_node is already running",
        "preset_refused": "nothing started",
    }
    for item in (*spec.ready, *spec.fail):
        if item.key not in wanted:
            continue
        match = re.fullmatch(r"controllers/fr3_serl/(.+):(\d+)", item.source)
        assert match, item.source
        assert match.group(1) == str(launch.relative_to(folder)), item.key
        assert wanted[item.key] in lines[int(match.group(2)) - 1], item.key
    # the same text, as the page's pattern reads it
    assert spec.ready[0].pattern.search(
        "[INFO] [launch.user]: anyspeedup arm controller, preset today"
    )


def test_the_launch_and_environment_file_take_what_the_command_gives_them():
    folder = anyspeedup_checkout()
    if folder is None:
        pytest.skip("no anyspeedup checkout beside this one")
    launch = (
        folder / "anyspeedup_franka_controllers" / "launch" / "impedance.launch.py"
    ).read_text(encoding="utf-8")
    for name in ("robot_type", "robot_ip", "load_gripper", "preset"):
        assert f'DeclareLaunchArgument("{name}"' in launch, name
    env = (folder / "env.sh").read_text(encoding="utf-8")
    assert "fr3_env.sh" in env and "ws/install/setup.bash" in env
    assert (folder / "build.sh").is_file()
    # the preset names the launch takes are the ones this page offers (and S2)
    assert 'PRESETS = ("today", "S1", "S2")' in launch
    assert (
        stack_units.ANYSPEEDUP_PACKAGE in launch
        or (folder / stack_units.ANYSPEEDUP_PACKAGE).is_dir()
    )


# --- A's start --------------------------------------------------------------------------------


@pytest.fixture
def m(tmp_path):
    return ActMachine(tmp_path)


def fake_checkout(tmp_path, env=True, build=True, mode=0o644):
    root = tmp_path / "anyspeedup"
    folder = root / "controllers" / "fr3_serl"
    folder.mkdir(parents=True)
    if env:
        (folder / "env.sh").write_text("")
        (folder / "env.sh").chmod(mode)
    if build:
        install = folder / "ws" / "install"
        (install / "anyspeedup_franka_controllers").mkdir(parents=True)
        (install / "setup.bash").write_text("")
    return root


def a_record(m, key="a"):
    return record.read_record(m.state_root / key)


def choose(m, root, preset="S1"):
    m.set_control(
        arm_controller="anyspeedup", arm_preset=preset, anyspeedup_root=str(root)
    )


def test_with_the_lab_controller_nothing_of_anyspeedup_is_asked(m):
    m.ready_for_a()
    assert m.terminal("a")["can_start"] is True
    assert not {
        "arm_controller_missing",
        "arm_controller_not_built",
    } & set(m.blockers("a"))
    m.act.start("a", m.start_body("a"))
    [argv] = m.systemd_runs("a")
    tail = argv[argv.index("--") + 1 :]
    assert tail[2] == str(m.franka / "env" / "fr3_env.sh")
    assert "serl_franka_controllers_ros2" in tail
    assert "anyspeedup_franka_controllers" not in tail


def test_the_anyspeedup_unit_starts_when_its_checkout_and_build_are_there(m, tmp_path):
    choose(m, fake_checkout(tmp_path), "S1")
    m.ready_for_a()
    assert m.terminal("a")["can_start"] is True, m.blockers("a")
    m.act.start("a", m.start_body("a"))
    [argv] = m.systemd_runs("a")
    tail = argv[argv.index("--") + 1 :]
    assert tail[2] == str(tmp_path / "anyspeedup/controllers/fr3_serl/env.sh")
    assert list(tail[3:]) == [
        "--",
        "ros2",
        "launch",
        "--noninteractive",
        "anyspeedup_franka_controllers",
        "impedance.launch.py",
        "robot_type:=fr3",
        "robot_ip:=10.42.0.157",
        "load_gripper:=false",
        "preset:=S1",
    ]
    # env.sh is told where the rig's environment file is (the control file's
    # franka_control), through the unit's environment: the wrapper is untouched
    assert f"--setenv=ANYSPEEDUP_FR3_ENV={m.franka}/env/fr3_env.sh" in argv
    # and the record says which arm controller this A is
    record_a = a_record(m)
    assert (record_a["arm_controller"], record_a["arm_preset"]) == (
        "anyspeedup",
        "S1",
    )
    assert record_a["arm_env"] == str(
        tmp_path / "anyspeedup/controllers/fr3_serl/env.sh"
    )


def test_the_lab_records_its_controller_and_asks_for_no_environment(m):
    m.start_a()
    [argv] = m.systemd_runs("a")
    assert not [t for t in argv if t.startswith("--setenv=ANYSPEEDUP")]
    record_a = a_record(m)
    assert (record_a["arm_controller"], record_a["arm_preset"]) == ("lab", None)
    assert record_a["arm_env"] is None
    # the other units' records do not carry the field
    m.start_b()
    assert "arm_controller" not in a_record(m, "b")


@pytest.mark.parametrize("mode", [0o664, 0o666, 0o646])
def test_an_environment_file_others_can_write_refuses_the_start(m, tmp_path, mode):
    choose(m, fake_checkout(tmp_path, mode=mode))
    m.ready_for_a()
    assert "arm_controller_unsafe" in m.blockers("a")
    assert "arm_controller_missing" not in m.blockers("a")
    with pytest.raises(stack.Refuse) as caught:
        m.act.start("a", m.start_body("a"))
    assert caught.value.code == "arm_controller_unsafe"
    assert not m.systemd_runs()
    row = next(
        b
        for b in m.terminal("a")["start_blockers"]
        if b["code"] == "arm_controller_unsafe"
    )
    assert "regular file" in row["text"] and str(tmp_path) not in row["text"]
    assert "arm_controller_unsafe" not in m.blockers("b")


def test_an_environment_file_that_is_a_link_refuses_the_start(m, tmp_path):
    root = fake_checkout(tmp_path)
    env = root / "controllers" / "fr3_serl" / "env.sh"
    target = tmp_path / "elsewhere.sh"
    target.write_text("")
    target.chmod(0o644)
    env.unlink()
    env.symlink_to(target)
    choose(m, root)
    m.ready_for_a()
    assert "arm_controller_unsafe" in m.blockers("a")


def test_a_file_check_that_cannot_look_refuses_too(m, tmp_path):
    choose(m, fake_checkout(tmp_path))
    m.ready_for_a()
    assert m.terminal("a")["can_start"] is True
    m.act.host.file_safe = lambda path: None
    assert "arm_controller_unsafe" in m.blockers("a")


def test_the_environment_file_check_is_the_control_files(tmp_path):
    safe = tmp_path / "ok.sh"
    safe.write_text("")
    safe.chmod(0o644)
    assert stack_act.file_safe(str(safe)) is True
    safe.chmod(0o620)
    assert stack_act.file_safe(str(safe)) is False
    assert stack_act.file_safe(str(tmp_path / "absent.sh")) is True, (
        "(the missing row says so)"
    )
    assert stack_act.file_safe(str(tmp_path)) is False, "a folder is not a file"


def test_a_partial_build_without_setup_bash_is_not_built(m, tmp_path):
    root = fake_checkout(tmp_path)
    (root / "controllers/fr3_serl/ws/install/setup.bash").unlink()
    choose(m, root)
    m.ready_for_a()
    assert "arm_controller_not_built" in m.blockers("a")
    # the package folder alone is not enough either, nor setup.bash alone
    root2 = fake_checkout(tmp_path / "second")
    import shutil

    shutil.rmtree(
        root2 / "controllers/fr3_serl/ws/install/anyspeedup_franka_controllers"
    )
    choose(m, root2)
    assert "arm_controller_not_built" in m.blockers("a")


def test_a_missing_environment_file_refuses_the_start_of_a_only(m, tmp_path):
    choose(m, fake_checkout(tmp_path, env=False))
    m.ready_for_a()
    assert "arm_controller_missing" in m.blockers("a")
    assert m.terminal("a")["can_start"] is False
    with pytest.raises(stack.Refuse):
        m.act.start("a", m.start_body("a"))
    assert not m.systemd_runs()
    row = next(
        b
        for b in m.terminal("a")["start_blockers"]
        if b["code"] == "arm_controller_missing"
    )
    assert "anyspeedup_root" in row["text"]
    assert "arm_controller_missing" not in m.blockers("b")
    assert "arm_controller_missing" not in m.blockers("c")


def test_a_missing_build_refuses_the_start_with_the_hint_to_build(m, tmp_path):
    choose(m, fake_checkout(tmp_path, build=False))
    m.ready_for_a()
    assert "arm_controller_not_built" in m.blockers("a")
    assert "arm_controller_missing" not in m.blockers("a")
    with pytest.raises(stack.Refuse):
        m.act.start("a", m.start_body("a"))
    assert not m.systemd_runs()
    row = next(
        b
        for b in m.terminal("a")["start_blockers"]
        if b["code"] == "arm_controller_not_built"
    )
    assert "build.sh" in row["text"]
    assert "arm_controller_not_built" not in m.blockers("b")
    assert stack_act.TEXT["arm_controller_not_built"][0] == 409


def test_a_checkout_that_disappears_blocks_the_start_again(m, tmp_path):
    root = fake_checkout(tmp_path)
    choose(m, root)
    m.ready_for_a()
    assert m.terminal("a")["can_start"] is True
    (root / "controllers" / "fr3_serl" / "env.sh").unlink()
    assert "arm_controller_missing" in m.blockers("a")


def test_a_started_anyspeedup_unit_is_ready_after_its_start_line(m, tmp_path):
    choose(m, fake_checkout(tmp_path), "S1")
    m.ready_for_a()
    m.act.start("a", m.start_body("a"))
    m.emit("a", *ready_lines("a"))
    m.health(hardware=True, controller=False, mode="IDLE")
    assert m.terminal("a")["state"] == "starting", (
        "the lab's lines alone are not enough"
    )
    m.emit("a", START.format(preset="S1"))
    assert m.terminal("a")["state"] == "ready"


def test_a_duplicate_message_of_the_anyspeedup_launch_fails_the_unit(m, tmp_path):
    choose(m, fake_checkout(tmp_path), "today")
    m.ready_for_a()
    m.act.start("a", m.start_body("a"))
    m.emit("a", DUPLICATE)
    m.health(hardware=True, controller=False, mode="IDLE")
    t = m.terminal("a")
    assert (t["state"], t["failure"]) == ("failed", "duplicate_launch")


def test_the_document_says_which_arm_controller_the_stack_is_configured_for(
    m, tmp_path
):
    assert m.doc()["control"]["arm"] == {
        "controller": "lab",
        "preset": None,
        "alpha": None,
    }
    choose(m, fake_checkout(tmp_path), "S1")
    arm = m.doc()["control"]["arm"]
    assert arm == {"controller": "anyspeedup", "preset": "S1", "alpha": 0.01}
    assert str(tmp_path) not in repr(m.doc()["control"]), "no path in the control view"


def test_the_catalog_lists_the_new_codes_for_the_front_end():
    cat = stack_act.catalog()
    assert {"arm_controller_missing", "arm_controller_not_built"} <= set(
        cat["blockers"]
    )
    assert "preset_refused" in cat["failures"]
    assert "anyspeedup_start" in cat["readiness"]


def test_the_guided_start_is_refused_up_front_with_the_reason_when_the_build_is_missing(
    m, tmp_path
):
    choose(m, fake_checkout(tmp_path, build=False))
    with pytest.raises(stack.Refuse) as caught:
        m.act.sequence_start(m.chain_body())
    assert caught.value.code == "arm_controller_not_built"
    assert "build.sh" in str(caught.value)
    assert not m.systemd_runs()
    # the reason is a code the page words (blocker catalogue, both languages)
    assert "arm_controller_not_built" in stack_act.catalog()["blockers"]
    assert m.doc()["sequence"]["state"] == "idle"
