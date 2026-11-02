"""The arm controller that RUNS against the one the control file names.

The control file can be changed while A runs. What the page says about A, the
alpha rule v3 is forced to and the labels of a run must follow the A that was
started (its record), not the file: a file edited after the start must neither
leave the card saying "ready" for the file's controller nor force the alpha of a
controller that is not on the arm. One test per scenario of the review's probe
(2026-11-02), then the foreign A, the quiet lab flow, and the document that
carries the answer (``act.arm``)."""

import pytest
from arm_controller_helpers import ANY_ROOT  # noqa: F401 - (the root the files name)
from stack_act_machine import ActMachine, ready_lines
from test_marvel0_arm_controller_unit import a_record, fake_checkout
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard

from levi.marvel0 import record

START = "[INFO] [launch.user]: anyspeedup arm controller, preset {preset}"


@pytest.fixture
def m(tmp_path):
    return ActMachine(tmp_path)


def anyspeedup(m, tmp_path, preset):
    root = tmp_path / "anyspeedup"
    if not root.exists():
        fake_checkout(tmp_path)
    m.set_control(
        arm_controller="anyspeedup", arm_preset=preset, anyspeedup_root=str(root)
    )


def a_ready(m, tmp_path=None, preset=None):
    """A ready: the lab's (``preset`` None) or the anyspeedup one."""
    if preset is not None:
        anyspeedup(m, tmp_path, preset)
    m.ready_for_a()
    m.act.start("a", m.start_body("a"))
    if preset is not None:
        m.emit("a", START.format(preset=preset))
    m.emit("a", *ready_lines("a"))
    m.health(hardware=True, controller=False, mode="IDLE")
    assert m.terminal("a")["state"] == "ready"


def arm(m):
    return m.doc()["arm"]


def terminal(m):
    t = m.terminal("a")
    return t["state"], t["reason"]


# --- the four rows of the review's probe ----------------------------------------------------


def test_a_lab_a_that_was_ready_is_not_ready_once_the_file_names_anyspeedup(
    m, tmp_path
):
    a_ready(m)
    assert arm(m)["status"] == "match" and arm(m)["alpha"] is None
    anyspeedup(m, tmp_path, "S1")
    assert terminal(m) == ("unknown", "a.control_changed")
    got = arm(m)
    assert got["running"] == {"controller": "lab", "preset": None}
    assert got["configured"] == {"controller": "anyspeedup", "preset": "S1"}
    assert got["status"] == "changed" and got["problem"] == "changed"
    # nothing is forced for a lab controller, whatever the file says
    assert got["alpha"] is None
    # B and C wait for a ready A; A is still the page's to stop
    assert m.terminal("a")["can_stop"] is True
    assert "a_not_ready" in m.blockers("b")


def test_an_anyspeedup_a_on_today_is_not_ready_once_the_file_says_s1(m, tmp_path):
    a_ready(m, tmp_path, "today")
    assert arm(m)["alpha"] == 0.005 and arm(m)["status"] == "match"
    anyspeedup(m, tmp_path, "S1")
    assert terminal(m) == ("unknown", "a.control_changed")
    got = arm(m)
    assert got["running"] == {"controller": "anyspeedup", "preset": "today"}
    assert got["configured"] == {"controller": "anyspeedup", "preset": "S1"}
    assert got["status"] == "changed" and got["problem"] == "changed"
    # not the file's 0.01, and not forced at all: the start of rule v3 is refused
    assert got["alpha"] is None


def test_an_anyspeedup_a_is_not_ready_once_the_file_says_lab(m, tmp_path):
    a_ready(m, tmp_path, "S1")
    m.set_control(arm_controller="lab")
    assert terminal(m) == ("unknown", "a.control_changed")
    got = arm(m)
    assert got["running"] == {"controller": "anyspeedup", "preset": "S1"}
    assert got["configured"] == {"controller": "lab", "preset": None}
    assert got["status"] == "changed" and got["problem"] == "changed"
    assert got["alpha"] is None, "(the lab's default would be 0.005: not forced either)"


def test_an_anyspeedup_a_with_the_stack_switched_off_is_still_what_it_was(m, tmp_path):
    a_ready(m, tmp_path, "S1")
    anyspeedup(m, tmp_path, "S1")
    m.set_control(
        enabled=False,
        arm_controller="anyspeedup",
        arm_preset="S1",
        anyspeedup_root=str(tmp_path / "anyspeedup"),
    )
    assert terminal(m) == ("unknown", "a.no_specs")
    got = arm(m)
    assert got["enabled"] is False
    assert got["running"] == {"controller": "anyspeedup", "preset": "S1"}
    assert got["status"] == "changed" and got["problem"] == "changed"
    assert got["alpha"] is None
    assert m.terminal("a")["can_stop"] is True


def test_a_lab_a_that_is_still_starting_follows_the_same_rule(m, tmp_path):
    # (before: it stayed "starting" and then timed out; a file that names another
    # controller is a different thing the moment it is read)
    m.ready_for_a()
    m.act.start("a", m.start_body("a"))
    anyspeedup(m, tmp_path, "S1")
    m.emit("a", *ready_lines("a"))
    m.health(hardware=True, controller=False, mode="IDLE")
    assert terminal(m) == ("unknown", "a.control_changed")
    assert arm(m)["problem"] == "changed"


def test_the_file_changing_back_makes_the_same_a_ready_again(m, tmp_path):
    a_ready(m)
    anyspeedup(m, tmp_path, "S1")
    assert terminal(m) == ("unknown", "a.control_changed")
    m.set_control(arm_controller="lab")
    assert terminal(m) == ("ready", "a.ready")
    assert arm(m)["status"] == "match" and arm(m)["problem"] is None


def test_another_checkout_of_the_same_preset_is_another_a_too(m, tmp_path):
    a_ready(m, tmp_path, "today")
    other = tmp_path / "other"
    other.mkdir()
    fake_checkout(other)
    m.set_control(
        arm_controller="anyspeedup",
        arm_preset="today",
        anyspeedup_root=str(other / "anyspeedup"),
    )
    assert terminal(m) == ("unknown", "a.control_changed")


# --- an A the page did not start ------------------------------------------------------------


def test_a_hand_started_a_with_the_file_naming_anyspeedup_refuses_rule_v3(m, tmp_path):
    anyspeedup(m, tmp_path, "S1")
    m.hand_started_a()
    got = arm(m)
    assert got["status"] == "foreign" and got["running"] is None
    assert got["problem"] == "foreign"
    assert got["alpha"] is None, "(before: forced to 0.01 on a controller nobody knows)"
    assert m.terminal("a")["state"] == "foreign_running"


def test_a_hand_started_a_with_the_lab_file_is_the_usual_flow_and_changes_nothing(m):
    m.hand_started_a()
    got = arm(m)
    assert got["status"] == "foreign"
    assert got["problem"] is None and got["alpha"] is None and got["ramp_ok"] is True
    assert got["suffix"] == ""


def test_an_a_that_could_not_be_judged_with_the_anyspeedup_file_refuses_rule_v3(
    m, tmp_path
):
    anyspeedup(m, tmp_path, "today")
    m.show_failing = {"ActiveState"}  # (systemd cannot be asked)
    got = arm(m)
    assert got["status"] == "unknown" and got["problem"] == "unknown"
    assert got["alpha"] is None


# --- the quiet cases and the document ---------------------------------------------------------


def test_a_lab_a_with_the_lab_file_asks_nothing_of_the_command(m):
    a_ready(m)
    assert arm(m) == {
        "configured": {"controller": "lab", "preset": None},
        "enabled": True,
        "running": {"controller": "lab", "preset": None},
        "status": "match",
        "alpha": None,
        "problem": None,
        "ramp_ok": True,
        "suffix": "",
    }


@pytest.mark.parametrize(
    "preset, alpha, ramp_ok", [("today", 0.005, True), ("S1", 0.01, False)]
)
def test_an_anyspeedup_a_that_matches_the_file_forces_its_presets_alpha(
    m, tmp_path, preset, alpha, ramp_ok
):
    a_ready(m, tmp_path, preset)
    assert arm(m) == {
        "configured": {"controller": "anyspeedup", "preset": preset},
        "enabled": True,
        "running": {"controller": "anyspeedup", "preset": preset},
        "status": "match",
        "alpha": alpha,
        "problem": None,
        "ramp_ok": ramp_ok,
        "suffix": f"_any-{preset}",
    }
    assert terminal(m) == ("ready", "a.ready")


def test_before_a_runs_the_file_is_what_the_page_will_start(m, tmp_path):
    anyspeedup(m, tmp_path, "S1")
    got = arm(m)
    assert got["status"] == "idle" and got["running"] is None
    assert got["alpha"] == 0.01 and got["problem"] is None
    assert got["ramp_ok"] is False and got["suffix"] == "_any-S1"
    # a stack that is off starts nothing: nothing is forced
    m.set_control(
        enabled=False,
        arm_controller="anyspeedup",
        arm_preset="S1",
        anyspeedup_root=str(tmp_path / "anyspeedup"),
    )
    got = arm(m)
    assert got["enabled"] is False and got["alpha"] is None
    assert got["suffix"] == "" and got["ramp_ok"] is True


def test_an_a_started_before_the_choice_existed_is_a_lab_a(m):
    a_ready(m)
    folder = m.state_root / "a"
    old = {
        k: v
        for k, v in a_record(m).items()
        if k not in ("arm_controller", "arm_preset", "arm_env")
    }
    with record.locked(folder):
        record.write_record(folder, old)
    assert "arm_controller" not in a_record(m)
    assert terminal(m) == ("ready", "a.ready")
    assert arm(m)["running"] == {"controller": "lab", "preset": None}


def test_a_unit_that_has_failed_or_is_gone_is_not_a_running_controller(m, tmp_path):
    a_ready(m, tmp_path, "S1")
    m.units["a"]["active"] = "failed"
    got = arm(m)
    assert got["running"] is None and got["status"] == "idle"
    # (what A will be started as is the file's)
    assert got["alpha"] == 0.01


def test_the_view_holds_no_path(m, tmp_path):
    a_ready(m, tmp_path, "S1")
    anyspeedup(m, tmp_path, "S1")
    assert str(tmp_path) not in repr(arm(m))
