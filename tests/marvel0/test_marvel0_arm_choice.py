"""The arm controller chosen on terminal A's card (``levi/marvel0/arm_choice.py``,
``stack_act.Act.choose_arm``): a closed list of three controllers, saved in the
page's own workspace and never in the control file (which stays the default),
changed only while A is stopped, and followed by everything downstream: A's
argv and record, ``act.arm`` (``match``, the forced alpha, ``ramp_ok``, the
rollout group's suffix) and ``arm_facts``. The machine is the fake one of
``stack_act_machine.py``: nothing is started, signalled or connected to."""

import json
import os
import stat

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from stack_act_machine import POSE, ActMachine, ready_lines
from test_marvel0_arm_controller_unit import fake_checkout
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard

from levi.marvel0 import (
    arm_choice,
    arm_facts,
    control_site,
    record,
    stack,
    stack_act,
    stack_act_api,
    stack_api,
)

START = "[INFO] [launch.user]: anyspeedup arm controller, preset {preset}"
BASE = "/api/levi/marvel0/stack"


@pytest.fixture
def m(tmp_path):
    return ActMachine(tmp_path)


@pytest.fixture
def root(tmp_path):
    return fake_checkout(tmp_path)


def with_root(m, root, **kw):
    """The file names the lab's controller (or ``kw``'s) and an anyspeedup
    checkout, so that every choice can be made."""
    m.set_control(anyspeedup_root=str(root), **kw)


_RIDS = iter(range(1, 10_000))


def body(controller, preset=None, rid=None):
    return {
        "request_id": rid or f"arm-choice-{next(_RIDS):05d}",
        "controller": controller,
        "preset": preset,
    }


def choose(m, controller, preset=None, rid=None):
    return m.act.choose_arm(body(controller, preset, rid))


def refused(m, controller, preset=None) -> str:
    with pytest.raises(stack.Refuse) as caught:
        choose(m, controller, preset)
    return caught.value.code


def arm(m):
    return m.doc()["arm"]


def saved(m):
    path = m.state_root / arm_choice.FOLDER / record.RECORD
    return json.loads(path.read_text()) if path.exists() else None


def a_ready(m, preset=None):
    """Start A as the page decides and bring it up (``preset``: the anyspeedup
    preset it prints, None for the lab's controller)."""
    m.ready_for_a()
    m.act.start("a", m.start_body("a"))
    if preset is not None:
        m.emit("a", START.format(preset=preset))
    m.emit("a", *ready_lines("a"))
    m.health(hardware=True, controller=False, mode="IDLE")
    assert m.terminal("a")["state"] == "ready", m.terminal("a")


def a_argv(m):
    [argv] = m.systemd_runs("a")
    return argv[argv.index("--") + 1 :]


# --- the list -----------------------------------------------------------------------------


def test_the_list_is_the_lab_controller_and_the_two_offered_presets():
    assert arm_choice.CHOICES == (
        ("lab", None),
        ("anyspeedup", "today"),
        ("anyspeedup", "S1"),
    )
    for controller, preset in arm_choice.CHOICES:
        if controller == "anyspeedup":
            assert preset in control_site.ARM_PRESETS
            assert preset not in control_site.ARM_PRESETS_REFUSED
            assert preset in control_site.ARM_PRESET_ALPHA
    assert {c for c, _ in arm_choice.CHOICES} == set(control_site.ARM_CONTROLLERS)


@pytest.mark.parametrize("pair", arm_choice.CHOICES)
def test_each_choice_on_the_list_is_accepted(pair):
    assert arm_choice.check(*pair) == pair


def test_the_default_is_no_choice():
    assert arm_choice.check("default", None) is None


def test_s2_is_refused_with_its_own_code():
    with pytest.raises(arm_choice.Invalid) as caught:
        arm_choice.check("anyspeedup", "S2")
    assert caught.value.code == "arm_choice_preset_refused"
    assert caught.value.params == {"preset": "S2", "reason": "needs_shaped_profile"}


@pytest.mark.parametrize(
    "controller, preset",
    [
        ("lab", "today"),
        ("lab", "S1"),
        ("anyspeedup", None),
        ("anyspeedup", "S3"),
        ("anyspeedup", "s1"),
        ("anyspeedup", "today "),
        ("Lab", None),
        ("default", "S1"),
        ("", None),
        (None, None),
        (1, None),
        ("anyspeedup", 1),
        ("serl_franka_controllers_ros2", None),
    ],
)
def test_anything_else_is_refused(controller, preset):
    with pytest.raises(arm_choice.Invalid) as caught:
        arm_choice.check(controller, preset)
    assert caught.value.code == "arm_choice_invalid"


@pytest.mark.parametrize(
    "controller, preset, code",
    [
        ("anyspeedup", "S2", "arm_choice_preset_refused"),
        ("anyspeedup", "S3", "arm_choice_invalid"),
        ("lab", "S1", "arm_choice_invalid"),
    ],
)
def test_the_page_refuses_what_is_not_on_the_list_and_saves_nothing(
    m, root, controller, preset, code
):
    with_root(m, root)
    assert refused(m, controller, preset) == code
    assert saved(m) is None
    assert m.systemd_runs() == []


def test_the_refusals_have_sentences_and_statuses():
    for code, status in (
        ("arm_choice_invalid", 400),
        ("arm_choice_preset_refused", 409),
        ("arm_choice_no_anyspeedup", 409),
        ("arm_choice_unreadable", 409),
        ("arm_choice_a_not_idle", 409),
        ("arm_choice_busy", 409),
    ):
        assert stack_act.TEXT[code][0] == status
        assert code in stack_act.catalog()["blockers"]


# --- the anyspeedup choices need the file's checkout ------------------------------------------


def test_without_anyspeedup_root_only_the_lab_controller_can_be_chosen(m):
    options = arm(m)["options"]
    assert [(o["controller"], o["preset"], o["allowed"]) for o in options] == [
        ("lab", None, True),
        ("anyspeedup", "today", False),
        ("anyspeedup", "S1", False),
    ]
    assert {o["code"] for o in options[1:]} == {"arm_choice_no_anyspeedup"}
    assert [o["default"] for o in options] == [True, False, False]
    assert refused(m, "anyspeedup", "S1") == "arm_choice_no_anyspeedup"
    assert saved(m) is None
    assert choose(m, "lab")["done"] == "arm_choice_set"


def test_the_file_default_is_marked_among_the_options(m, root):
    with_root(m, root, arm_controller="anyspeedup", arm_preset="S1")
    options = arm(m)["options"]
    assert [o["default"] for o in options] == [False, False, True]
    assert all(o["allowed"] for o in options)


# --- what A is started as -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "preset, alpha, ramp_ok", [("today", 0.005, True), ("S1", 0.01, False)]
)
def test_a_chosen_preset_is_what_a_starts_as_and_reads_match(
    m, root, preset, alpha, ramp_ok
):
    with_root(m, root)  # the file says the lab's controller
    assert choose(m, "anyspeedup", preset) == {
        "done": "arm_choice_set",
        "controller": "anyspeedup",
        "preset": preset,
    }
    idle = arm(m)
    assert idle["file"] == {"controller": "lab", "preset": None}
    assert idle["configured"] == {"controller": "anyspeedup", "preset": preset}
    assert idle["choice"]["state"] == "chosen"
    assert (idle["choice"]["controller"], idle["choice"]["preset"]) == (
        "anyspeedup",
        preset,
    )
    assert (idle["status"], idle["alpha"], idle["problem"]) == ("idle", alpha, None)
    assert idle["ramp_ok"] is ramp_ok and idle["suffix"] == f"_any-{preset}"
    a_ready(m, preset)
    tail = a_argv(m)
    assert "anyspeedup_franka_controllers" in tail and f"preset:={preset}" in tail
    assert tail[2] == str(root / "controllers" / "fr3_serl" / "env.sh")
    rec = record.read_record(m.state_root / "a")
    assert (rec["arm_controller"], rec["arm_preset"]) == ("anyspeedup", preset)
    running = arm(m)
    assert running["running"] == {"controller": "anyspeedup", "preset": preset}
    assert running["status"] == "match", "(not 'changed': the choice is the want)"
    assert (running["alpha"], running["problem"]) == (alpha, None)
    assert running["ramp_ok"] is ramp_ok and running["suffix"] == f"_any-{preset}"
    assert m.terminal("a")["reason"] == "a.ready"


def test_a_chosen_preset_overrides_the_files_preset(m, root):
    with_root(m, root, arm_controller="anyspeedup", arm_preset="S1")
    choose(m, "anyspeedup", "today")
    got = arm(m)
    assert got["file"] == {"controller": "anyspeedup", "preset": "S1"}
    assert got["configured"] == {"controller": "anyspeedup", "preset": "today"}
    assert (got["alpha"], got["ramp_ok"], got["suffix"]) == (0.005, True, "_any-today")
    a_ready(m, "today")
    assert "preset:=today" in a_argv(m)
    assert arm(m)["status"] == "match" and arm(m)["alpha"] == 0.005


def test_the_lab_controller_chosen_over_an_anyspeedup_file(m, root):
    with_root(m, root, arm_controller="anyspeedup", arm_preset="S1")
    choose(m, "lab")
    got = arm(m)
    assert got["configured"] == {"controller": "lab", "preset": None}
    assert (got["alpha"], got["ramp_ok"], got["suffix"]) == (None, True, "")
    # the lab's controller asks nothing of the anyspeedup checkout
    assert not {"arm_controller_missing", "arm_controller_not_built"} & set(
        m.blockers("a")
    )
    a_ready(m)
    tail = a_argv(m)
    assert "serl_franka_controllers_ros2" in tail
    assert "anyspeedup_franka_controllers" not in tail
    assert tail[2] == str(m.franka / "env" / "fr3_env.sh")
    rec = record.read_record(m.state_root / "a")
    assert (rec["arm_controller"], rec["arm_preset"], rec["arm_env"]) == (
        "lab",
        None,
        None,
    )
    assert arm(m)["status"] == "match" and arm(m)["alpha"] is None


def test_the_effective_specs_are_the_ones_the_page_builds(m, root):
    with_root(m, root)
    choose(m, "anyspeedup", "S1")
    _, specs = m.act._loaded()
    env = str(root / "controllers" / "fr3_serl" / "env.sh")
    assert specs.arm_identity == ("anyspeedup", "S1", env)
    # the setup itself (the control file) is not changed
    assert m.specs.arm_identity == ("lab", None, None)


def test_an_unbuilt_anyspeedup_choice_still_meets_the_start_check(m, tmp_path):
    bare = fake_checkout(tmp_path, build=False)
    with_root(m, bare)
    choose(m, "anyspeedup", "S1")
    m.ready_for_a()
    assert "arm_controller_not_built" in m.blockers("a")
    with pytest.raises(stack_act.ActRefuse) as caught:
        m.act.start("a", m.start_body("a"))
    assert caught.value.code == "arm_controller_not_built"
    assert m.systemd_runs("a") == []


# --- going back to the file's default ---------------------------------------------------------


def test_the_default_clears_the_choice_and_a_follows_the_file_again(m, root):
    with_root(m, root)
    choose(m, "anyspeedup", "S1")
    assert choose(m, "default") == {
        "done": "arm_choice_cleared",
        "controller": None,
        "preset": None,
    }
    assert saved(m)["choice"] is None and saved(m)["schema"] == arm_choice.SCHEMA
    got = arm(m)
    assert got["choice"]["state"] == "default"
    assert got["configured"] == got["file"] == {"controller": "lab", "preset": None}
    assert (got["alpha"], got["suffix"]) == (None, "")
    a_ready(m)
    assert "serl_franka_controllers_ros2" in a_argv(m)


def test_a_choice_follows_no_later_change_of_the_file(m, root):
    with_root(m, root)
    choose(m, "anyspeedup", "today")
    with_root(m, root, arm_controller="anyspeedup", arm_preset="S1")
    assert arm(m)["configured"] == {"controller": "anyspeedup", "preset": "today"}


# --- only while A is stopped -------------------------------------------------------------------


def unchanged(m, before):
    assert saved(m) == before
    assert m.act._loaded()[1].arm_identity == ("lab", None, None)


def test_refused_while_a_runs(m, root):
    with_root(m, root)
    a_ready(m)
    before = saved(m)
    got = arm(m)
    assert got["can_choose"] is False
    assert [b["code"] for b in got["choose_blockers"]] == ["arm_choice_a_not_idle"]
    assert got["choose_blockers"][0]["params"] == {"state": "ready"}
    assert refused(m, "anyspeedup", "S1") == "arm_choice_a_not_idle"
    assert refused(m, "default") == "arm_choice_a_not_idle"
    unchanged(m, before)
    # the card still says what runs
    assert got["running"] == {"controller": "lab", "preset": None}


def test_refused_while_a_is_starting(m, root):
    with_root(m, root)
    m.ready_for_a()
    m.act.start("a", m.start_body("a"))
    assert m.terminal("a")["state"] == "starting"
    assert refused(m, "anyspeedup", "S1") == "arm_choice_a_not_idle"
    unchanged(m, None)


def test_refused_while_an_a_the_page_did_not_start_runs(m, root):
    with_root(m, root)
    m.hand_started_a()
    assert m.terminal("a")["state"] == "foreign_running"
    assert refused(m, "anyspeedup", "today") == "arm_choice_a_not_idle"
    assert arm(m)["choose_blockers"][0]["params"] == {"state": "foreign_running"}
    unchanged(m, None)


def test_refused_while_a_cannot_be_judged(m, root):
    with_root(m, root)
    m.show_failing = {"ActiveState"}  # (systemd cannot be asked)
    assert m.terminal("a")["state"] == "unknown"
    assert refused(m, "lab") == "arm_choice_a_not_idle"
    unchanged(m, None)


def test_refused_while_a_failed_and_its_record_is_open(m, root):
    with_root(m, root)
    m.ready_for_a()
    m.act.start("a", m.start_body("a"))
    m.t += 10_000  # (no ready line ever came: the start timed out)
    assert m.terminal("a")["state"] == "failed"
    assert refused(m, "anyspeedup", "S1") == "arm_choice_a_not_idle"
    unchanged(m, None)


def test_refused_while_a_guided_start_is_in_progress(m, root):
    with_root(m, root)
    m.act.sequence_start(m.chain_body())
    assert m.doc()["sequence"]["state"] in ("running", "waiting_confirm")
    assert m.terminal("a")["state"] == "idle"
    assert refused(m, "anyspeedup", "S1") == "arm_choice_busy"
    assert "arm_choice_busy" in [b["code"] for b in arm(m)["choose_blockers"]]
    unchanged(m, None)


def test_allowed_again_once_a_is_stopped(m, root):
    with_root(m, root)
    a_ready(m)
    m.act.stop("a", m.stop_body("a"))
    m.sockets = {"state": "inactive", "detail": ""}
    m.t += 120  # (the arm's sockets have drained)
    assert m.terminal("a")["state"] == "idle", m.terminal("a")
    assert arm(m)["can_choose"] is True
    assert choose(m, "anyspeedup", "S1")["done"] == "arm_choice_set"


def test_refused_with_the_stack_switched_off(tmp_path):
    m = ActMachine(tmp_path, enabled=False)
    got = arm(m)
    assert got["can_choose"] is False
    assert [b["code"] for b in got["choose_blockers"]] == ["control_disabled"]
    assert refused(m, "lab") == "control_disabled"
    assert saved(m) is None


def test_refused_outside_the_product(tmp_path):
    m = ActMachine(tmp_path, product=False)
    assert "gate.product" in [b["code"] for b in arm(m)["choose_blockers"]]
    assert refused(m, "lab") == "not_product"
    assert saved(m) is None


# --- the record ----------------------------------------------------------------------------------


def test_the_choice_is_a_private_record_next_to_the_stacks_with_an_audit_line(m, root):
    with_root(m, root)
    choose(m, "anyspeedup", "S1", rid="arm-choice-audit1")
    folder = m.state_root / arm_choice.FOLDER
    assert stat.S_IMODE(os.lstat(folder).st_mode) == 0o700
    path = folder / record.RECORD
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    assert saved(m) == {
        "schema": arm_choice.SCHEMA,
        "choice": {"controller": "anyspeedup", "preset": "S1"},
        "request_id": "arm-choice-audit1",
        "at": round(m.t, 3),
    }
    choose(m, "default", rid="arm-choice-audit2")
    lines = [
        json.loads(line) for line in (folder / record.HISTORY).read_text().splitlines()
    ]
    assert [(x["action"], x["phase"], x["request_id"]) for x in lines] == [
        ("arm_choice", "chosen", "arm-choice-audit1"),
        ("arm_choice", "default", "arm-choice-audit2"),
    ]
    assert lines[0]["detail"] == "anyspeedup S1"


def test_a_request_id_is_answered_again_never_acted_on_twice(m, root):
    with_root(m, root)
    first = choose(m, "anyspeedup", "S1", rid="arm-choice-same1")
    again = choose(m, "anyspeedup", "S1", rid="arm-choice-same1")
    assert again == {**first, "repeated": True}
    with pytest.raises(stack.Refuse) as caught:
        choose(m, "lab", rid="arm-choice-same1")
    assert caught.value.code == "request_id_used"
    assert saved(m)["choice"] == {"controller": "anyspeedup", "preset": "S1"}


# What ``arm_choice.write`` writes for anyspeedup S1.
WRITTEN = {
    "schema": arm_choice.SCHEMA,
    "choice": {"controller": "anyspeedup", "preset": "S1"},
    "request_id": "arm-choice-written",
    "at": 1.5,
}


def test_read_takes_exactly_what_write_writes(tmp_path):
    folder = tmp_path / arm_choice.FOLDER
    folder.mkdir(mode=0o700)
    value = arm_choice.write(folder, ("anyspeedup", "S1"), "arm-choice-w1", 2.0)
    assert set(value) == arm_choice.KEYS
    assert arm_choice.read(tmp_path) == {
        "state": "chosen",
        "choice": ("anyspeedup", "S1"),
        "at": 2.0,
    }
    arm_choice.write(folder, None, None, 3.0)
    assert arm_choice.read(tmp_path) == {"state": "default", "choice": None, "at": 3.0}
    (folder / record.RECORD).write_text(json.dumps(WRITTEN))
    assert arm_choice.read(tmp_path)["state"] == "chosen"


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"schema": "levi.marvel0.arm_choice.v0", "choice": None}),
        json.dumps(
            {
                "schema": arm_choice.SCHEMA,
                "choice": {"controller": "anyspeedup", "preset": "S2"},
            }
        ),
        json.dumps({"schema": arm_choice.SCHEMA, "choice": {"controller": "lab"}}),
        json.dumps({"schema": arm_choice.SCHEMA}),
        # not exactly what ``write`` writes: an extra key, a missing one, or a
        # field of the wrong kind
        json.dumps({**WRITTEN, "junk": [1, 2]}),
        json.dumps({k: v for k, v in WRITTEN.items() if k != "request_id"}),
        json.dumps({k: v for k, v in WRITTEN.items() if k != "at"}),
        json.dumps({**WRITTEN, "at": "yesterday"}),
        json.dumps({**WRITTEN, "at": True}),
        json.dumps({**WRITTEN, "at": float("inf")}),
        json.dumps({**WRITTEN, "request_id": 7}),
    ],
)
def test_a_choice_that_cannot_be_read_keeps_a_from_starting_until_it_is_cleared(
    m, root, content
):
    with_root(m, root)
    folder = m.state_root / arm_choice.FOLDER
    folder.mkdir(parents=True, mode=0o700)
    (folder / record.RECORD).write_text(content)
    m.ready_for_a()
    got = arm(m)
    assert got["choice"]["state"] == "unreadable"
    assert got["configured"] == got["file"], "(shown as the file's, never a guess)"
    assert "arm_choice_unreadable" in m.blockers("a")
    with pytest.raises(stack_act.ActRefuse) as caught:
        m.act.start("a", m.start_body("a"))
    assert caught.value.code == "arm_choice_unreadable"
    assert m.systemd_runs("a") == []
    choose(m, "default")
    assert arm(m)["choice"]["state"] == "default"
    assert "arm_choice_unreadable" not in m.blockers("a")


def test_a_choice_the_file_no_longer_allows_keeps_a_from_starting(m, root):
    with_root(m, root)
    choose(m, "anyspeedup", "S1")
    m.set_control()  # the owner took anyspeedup_root out of the file
    got = arm(m)
    assert got["choice"]["state"] == "unusable"
    assert got["choice"]["code"] == "arm_choice_no_anyspeedup"
    assert got["configured"] == {"controller": "lab", "preset": None}
    m.ready_for_a()
    assert "arm_choice_no_anyspeedup" in m.blockers("a")
    with pytest.raises(stack_act.ActRefuse) as caught:
        m.act.start("a", m.start_body("a"))
    assert caught.value.code == "arm_choice_no_anyspeedup"
    choose(m, "lab")
    assert "arm_choice_no_anyspeedup" not in m.blockers("a")


@pytest.mark.parametrize("pair", arm_choice.CHOICES)
def test_effective_without_a_file_is_nothing_and_blames_no_choice(pair):
    """No valid control file: nothing is started and the card says the file's
    problem; a saved lab choice is not said to need anyspeedup_root."""
    stack_control, view = arm_choice.effective(
        None, {"state": "chosen", "choice": pair, "at": 1.0}
    )
    assert stack_control is None
    assert (view["state"], view["code"]) == ("chosen", None)
    assert (view["controller"], view["preset"]) == pair


def test_without_a_file_the_card_shows_no_problem_of_the_choice(m, root):
    with_root(m, root)
    choose(m, "lab")
    m.loaded = control_site.ControlLoad("missing", None)
    m.specs = None  # (as the real setup gives without a file)
    got = arm(m)
    assert got["configured"] is None and got["file"] is None
    assert got["choice"]["state"] == "chosen" and got["choice"]["code"] is None
    assert m.blockers("a") == ["control_disabled"]


# --- the control file is never written -------------------------------------------------------


def test_the_control_file_is_never_written(m, root, tmp_path, monkeypatch):
    rig = tmp_path / "rig"  # (the control file's own folder)
    rig.mkdir()
    path = rig / "control.toml"
    path.write_text(
        "\n".join(
            [
                f'schema = "{control_site.SCHEMA}"',
                "[stack]",
                "enabled = true",
                f'franka_control = "{m.franka}"',
                'robot_ip = "10.42.0.157"',
                'com_port = "/dev/serial/by-id/usb-FTDI_TEST-if00-port0"',
                "server_port = 5000",
                f"reset_joint_target = {list(POSE)}",
                'arm_controller = "lab"',
                f'anyspeedup_root = "{root}"',
                "",
            ]
        )
    )
    path.chmod(0o600)
    monkeypatch.setenv(control_site.ENV, str(path))
    control_site.clear_cache()
    m.act = stack_act.Act(m.sup, m.act_host(), setup=stack_act.default_setup)
    m.sup.act = m.act
    before = (path.read_bytes(), os.stat(path).st_mtime_ns, os.stat(path).st_ino)
    siblings = sorted(os.listdir(rig))
    choose(m, "anyspeedup", "S1")
    a_ready(m, "S1")
    assert "preset:=S1" in a_argv(m)
    assert arm(m)["file"] == {"controller": "lab", "preset": None}
    assert arm(m)["status"] == "match"
    after = (path.read_bytes(), os.stat(path).st_mtime_ns, os.stat(path).st_ino)
    assert after == before
    assert sorted(os.listdir(rig)) == siblings
    assert control_site.load().stack.arm_controller == "lab"
    control_site.clear_cache()


# --- arm_facts: the client command's side ----------------------------------------------------------


def test_the_command_builder_gets_the_chosen_presets_alpha(m, root, monkeypatch):
    monkeypatch.setattr(stack_api, "supervisor", lambda: m.sup)
    monkeypatch.setattr(stack_api, "_VIEW", [])
    with_root(m, root)
    choose(m, "anyspeedup", "S1")
    got = arm_facts.arm_of(arm_facts.read(fresh=True))
    assert (got.alpha, got.ramp_ok, got.suffix, got.anyspeedup) == (
        0.01,
        False,
        "_any-S1",
        True,
    )
    choose(m, "anyspeedup", "today")
    got = arm_facts.arm_of(arm_facts.read(fresh=True))
    assert (got.alpha, got.ramp_ok, got.suffix) == (0.005, True, "_any-today")


@pytest.mark.parametrize(
    "file, pick, status, ramp_ok",
    [
        ({}, ("anyspeedup", "S1"), "unknown", False),
        ({}, ("anyspeedup", "today"), "unknown", True),
        (
            {"arm_controller": "anyspeedup", "arm_preset": "S1"},
            ("lab", None),
            "idle",
            True,
        ),
        (
            {"arm_controller": "anyspeedup", "arm_preset": "today"},
            None,
            "unknown",
            True,
        ),
    ],
)
def test_the_file_fallback_of_arm_facts_reads_the_choice_too(
    m, root, monkeypatch, file, pick, status, ramp_ok
):
    with_root(m, root, **file)
    if pick is not None:
        choose(m, *pick)
    monkeypatch.setattr(stack_act, "_state_root", lambda: m.state_root)
    monkeypatch.setattr(control_site, "read", lambda *a, **k: m.loaded)
    got = arm_facts.from_file()
    assert (got["status"], got["ramp_ok"], got["alpha"]) == (status, ramp_ok, None)


@pytest.mark.parametrize(
    "file, broken",
    [
        ({}, "unreadable"),
        ({}, "unusable"),  # (only a lab file can lose anyspeedup_root)
        ({}, "no_folder"),
        ({"arm_controller": "anyspeedup", "arm_preset": "today"}, "unreadable"),
        ({"arm_controller": "anyspeedup", "arm_preset": "today"}, "no_folder"),
    ],
)
def test_the_file_fallback_says_unknown_when_the_choice_cannot_be_read_or_used(
    m, root, monkeypatch, file, broken
):
    """A page-started S1 A runs, then the saved choice breaks (its record
    corrupted, the file's anyspeedup_root taken out, or the state folder
    unreadable) and the stack document cannot be had: the fallback must not
    answer with the file's controller (ramp rules on, nothing refused), since
    which controller A runs is not known without A's record."""
    with_root(m, root, **file)
    choose(m, "anyspeedup", "S1")
    a_ready(m, "S1")
    if broken == "unreadable":
        (m.state_root / arm_choice.FOLDER / record.RECORD).write_text("not json")
    elif broken == "unusable":
        m.set_control(**file)  # (anyspeedup_root taken out of the file)
    monkeypatch.setattr(control_site, "read", lambda *a, **k: m.loaded)
    if broken == "no_folder":

        def gone():
            raise OSError("no workspace")

        monkeypatch.setattr(stack_act, "_state_root", gone)
    else:
        monkeypatch.setattr(stack_act, "_state_root", lambda: m.state_root)
    got = arm_facts.from_file()
    assert (got["status"], got["problem"], got["ramp_ok"], got["alpha"]) == (
        "unknown",
        "unknown",
        False,
        None,
    )
    facts = arm_facts.arm_of(got)
    assert (facts.alpha, facts.ramp_ok) == (None, False)
    assert arm_facts.problem_of(got) == "unknown"


def test_the_file_fallback_with_a_broken_choice_and_the_stack_off_asks_nothing(
    m, root, monkeypatch
):
    with_root(m, root)
    folder = m.state_root / arm_choice.FOLDER
    folder.mkdir(parents=True, mode=0o700)
    (folder / record.RECORD).write_text("not json")
    m.set_control(anyspeedup_root=str(root), enabled=False)
    monkeypatch.setattr(stack_act, "_state_root", lambda: m.state_root)
    monkeypatch.setattr(control_site, "read", lambda *a, **k: m.loaded)
    got = arm_facts.from_file()
    assert (got["status"], got["problem"], got["ramp_ok"]) == ("idle", None, True)


# --- the route --------------------------------------------------------------------------------------


@pytest.fixture
def client(m, monkeypatch):
    monkeypatch.setattr(stack_api, "supervisor", lambda: m.sup)
    monkeypatch.setattr(stack_api, "_VIEW", [])
    monkeypatch.delenv("LEVI_UI_TOKEN", raising=False)
    web = FastAPI()
    web.include_router(stack_api.router)
    web.include_router(stack_act_api.router)
    return TestClient(web)


def test_the_route_saves_the_choice_and_the_next_read_shows_it(client, m, root):
    with_root(m, root)
    assert client.get(BASE).json()["act"]["arm"]["choice"]["state"] == "default"
    answer = client.post(
        f"{BASE}/a/controller",
        json={
            "request_id": "route-choice-1",
            "controller": "anyspeedup",
            "preset": "S1",
        },
    )
    assert answer.status_code == 200, answer.text
    assert answer.json() == {
        "done": "arm_choice_set",
        "controller": "anyspeedup",
        "preset": "S1",
    }
    shown = client.get(BASE).json()["act"]["arm"]
    assert shown["configured"] == {"controller": "anyspeedup", "preset": "S1"}
    assert shown["choice"]["state"] == "chosen"
    cleared = client.post(
        f"{BASE}/a/controller",
        json={"request_id": "route-choice-2", "controller": "default"},
    )
    assert cleared.status_code == 200 and cleared.json()["done"] == "arm_choice_cleared"


@pytest.mark.parametrize(
    "payload, status, code",
    [
        (
            {"controller": "anyspeedup", "preset": "S2"},
            409,
            "arm_choice_preset_refused",
        ),
        ({"controller": "anyspeedup", "preset": "S9"}, 400, "arm_choice_invalid"),
        ({"controller": "lab", "preset": "S1"}, 400, "arm_choice_invalid"),
    ],
)
def test_the_route_refuses_off_the_list_with_its_code(
    client, m, root, payload, status, code
):
    with_root(m, root)
    answer = client.post(
        f"{BASE}/a/controller", json={"request_id": "route-choice-9", **payload}
    )
    assert answer.status_code == status, answer.text
    assert answer.json()["detail"]["code"] == code
    assert saved(m) is None


def test_the_route_refuses_while_a_runs(client, m, root):
    with_root(m, root)
    a_ready(m)
    answer = client.post(
        f"{BASE}/a/controller",
        json={
            "request_id": "route-choice-3",
            "controller": "anyspeedup",
            "preset": "S1",
        },
    )
    assert answer.status_code == 409
    detail = answer.json()["detail"]
    assert detail["code"] == "arm_choice_a_not_idle" and detail["state"] == "ready"
    assert saved(m) is None


# --- A's start is the controller its window named ------------------------------------------------


def named(controller, preset=None):
    """What A's start window names (``act.arm.configured``)."""
    return {"controller": controller, "preset": preset}


def test_a_start_with_the_named_controller_starts_it(m, root):
    with_root(m, root)
    choose(m, "anyspeedup", "S1")
    window = arm(m)["configured"]
    assert window == named("anyspeedup", "S1")
    m.ready_for_a()
    m.act.start("a", {**m.start_body("a"), "arm": window})
    assert "preset:=S1" in a_argv(m)


def test_a_start_is_refused_when_the_choice_changed_after_its_window_opened(m, root):
    """The window was read from the last poll; another tab chose another
    controller meanwhile: A is not started as one the person did not confirm."""
    with_root(m, root)
    choose(m, "anyspeedup", "S1")
    window = arm(m)["configured"]
    choose(m, "lab")  # (another tab)
    m.ready_for_a()
    with pytest.raises(stack_act.ActRefuse) as caught:
        m.act.start("a", {**m.start_body("a"), "arm": window})
    assert caught.value.code == "arm_choice_changed"
    assert m.systemd_runs("a") == []
    # the window read again names the lab's controller, which then starts
    m.act.start(
        "a", {**m.start_body("a", rid="start-a-0002"), "arm": arm(m)["configured"]}
    )
    assert "serl_franka_controllers_ros2" in a_argv(m)


@pytest.mark.parametrize(
    "said",
    [
        named("anyspeedup", "today"),  # the preset differs
        named("lab", "S1"),
        {"controller": "anyspeedup"},  # (no preset: the lab's has none)
        "anyspeedup",
        [],
    ],
)
def test_a_start_naming_anything_else_is_refused(m, root, said):
    with_root(m, root)
    choose(m, "anyspeedup", "S1")
    m.ready_for_a()
    with pytest.raises(stack_act.ActRefuse) as caught:
        m.act.start("a", {**m.start_body("a"), "arm": said})
    assert caught.value.code == "arm_choice_changed"
    assert m.systemd_runs("a") == []


def test_a_start_from_an_older_page_without_the_field_starts_as_before(m, root):
    with_root(m, root)
    choose(m, "anyspeedup", "today")
    m.ready_for_a()
    m.act.start("a", m.start_body("a"))  # (no "arm")
    assert "preset:=today" in a_argv(m)


def test_the_guided_start_is_refused_at_once_when_the_window_named_another(m, root):
    with_root(m, root)
    choose(m, "anyspeedup", "S1")
    with pytest.raises(stack_act.ActRefuse) as caught:
        m.act.sequence_start(m.chain_body(arm=named("lab")))
    assert caught.value.code == "arm_choice_changed"
    assert m.doc()["sequence"]["state"] == "idle"
    assert m.systemd_runs() == []


def test_the_guided_start_checks_the_named_controller_again_when_it_starts_a(m, root):
    """The chain starts A minutes after the click (the marker's wait). The
    choice cannot change meanwhile, but the control file can: A is then not
    started as a controller the window did not name."""
    with_root(m, root)
    m.act.sequence_start(m.chain_body(arm=arm(m)["configured"]))
    seq = m.run_chain(stop_at=lambda s: s.get("step") == "arm")
    assert seq["step"] == "arm" and m.systemd_runs("a") == []
    with_root(m, root, arm_controller="anyspeedup", arm_preset="today")
    seq = m.run_chain()
    assert (seq["state"], seq["reason"]) == ("failed", "arm_choice_changed")
    assert m.systemd_runs("a") == []


def test_the_guided_start_with_the_named_controller_starts_a_as_it(m, root):
    with_root(m, root)
    choose(m, "anyspeedup", "S1")
    m.act.sequence_start(m.chain_body(arm=arm(m)["configured"]))
    m.run_chain(stop_at=lambda s: s.get("step") == "await_a")
    assert "preset:=S1" in a_argv(m)


def test_the_route_of_a_start_carries_the_named_controller(client, m, root):
    with_root(m, root)
    choose(m, "anyspeedup", "S1")
    m.ready_for_a()
    answer = client.post(
        f"{BASE}/a/start",
        json={**m.start_body("a", rid="route-start-a1"), "arm": named("lab")},
    )
    assert answer.status_code == 409, answer.text
    assert answer.json()["detail"]["code"] == "arm_choice_changed"
    assert m.systemd_runs("a") == []
    # B and C name no arm controller: their closed bodies refuse the field
    other = client.post(
        f"{BASE}/b/start",
        json={**m.start_body("b", rid="route-start-b1"), "arm": named("lab")},
    )
    assert other.status_code == 422
