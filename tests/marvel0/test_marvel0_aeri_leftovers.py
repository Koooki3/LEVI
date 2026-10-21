"""What an AERI run leaves behind and how the card stops it
(``levi/marvel0/aeri_leftovers.py``): which units are leftovers (and which are
never touched), the order of the reasons a stop is withheld, ``systemctl stop``
as it really behaves, and the stop of the live annotation service
(``systemctl --user stop`` of its unit, with an allowlisted environment)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from aeri_world import DEFAULT_PROFILES, World, installed_profiles

from levi.marvel0 import aeri_leftovers as left
from levi.marvel0 import aeri_prep
from levi.marvel0.aeri_prep import Refuse


@pytest.fixture
def world(tmp_path, monkeypatch):
    installed_profiles(monkeypatch, DEFAULT_PROFILES)
    return World(tmp_path)


def item(name, active="active", sub="running"):
    return {"name": name, "active": active, "sub": sub}


def test_the_patterns_are_the_ones_the_run_and_its_units_use():
    from levi.automatic import subtask_units
    from levi.own_units import POLICY_UNIT

    assert left.VLM_UNIT.pattern == subtask_units.VLM_UNIT.pattern
    assert left.POLICY_UNIT.pattern == POLICY_UNIT.pattern


@pytest.mark.parametrize(
    "name, kind",
    [
        ("levi-aeri-vlm-8100.service", "vlm"),
        ("levi-aeri-vlm-8110.service", "vlm"),
        ("levi-aeri-policy-forward.service", "policy"),
        ("levi-aeri-policy-reset.service", "policy"),
    ],
)
def test_the_units_a_run_leaves_are_leftovers(name, kind):
    assert left.leftover_units([item(name)]) == [
        {"name": name, "kind": kind, "active": "active", "sub": "running"}
    ]


@pytest.mark.parametrize(
    "name",
    [
        # a policy server started by hand, or by this page's deploy: not ours to stop here
        "levi-aeri-policy-manual-eggplant.service",
        "levi-aeri-policy-manual-eggplant.scope",
        "levi-aeri-policy-manual-marvel0-pi05.service",
        # scopes are processes a person or the page runs, never what a run leaves
        "levi-aeri-vlm-8100.scope",
        "levi-aeri-policy-forward.scope",
        # the other members of the family
        "levi-aeri-client-marvel0-20261021T192459-54e767.scope",
        "levi-aeri-termd-marvel0-20261021T200000-abcdef.scope",
        "levi-aeri-20261021-ext-r01.service",
        "levi-live.service",
        "levi-product.service",
        # names that only look like them
        "levi-aeri-vlm-.service",
        "levi-aeri-vlm-8100;x.service",
        "levi-aeri-vlm-8100",
        "xlevi-aeri-vlm-8100.service",
        "",
    ],
)
def test_nothing_else_is_ever_offered_for_stopping(name):
    assert left.leftover_units([item(name)]) == []


def test_every_aeri_unit_counts_for_the_readiness_row_but_not_for_stopping():
    names = [
        "levi-aeri-vlm-8100.service",
        "levi-aeri-policy-manual-eggplant.scope",
        "levi-aeri-client-marvel0-1.scope",
        "levi-live.service",
    ]
    assert left.any_aeri_unit([item(n) for n in names]) == names[:2]


def test_missing_fields_do_not_crash_the_selection():
    assert left.leftover_units([{}, {"name": None}, {"name": 5}]) == []
    assert left.any_aeri_unit([{}, {"name": None}]) == []
    got = left.leftover_units([{"name": "levi-aeri-vlm-1.service"}])
    assert got == [
        {"name": "levi-aeri-vlm-1.service", "kind": "vlm", "active": None, "sub": None}
    ]


def test_an_unreadable_unit_list_is_none_not_empty():
    assert left.units_items({"state": "unknown", "items": []}) is None
    assert left.units_items({"state": "ok", "items": "x"}) is None
    assert left.units_items(None) is None
    assert left.units_items({"state": "ok", "items": []}) == []


# --- why a stop is withheld --------------------------------------------------------------


def test_the_reasons_come_in_a_fixed_order():
    assert left.why_not(d_alive=False, e_active=False, arm_held=False) is None
    both = left.why_not(d_alive=True, e_active=True, arm_held=True, gpu_held=True)
    assert both["code"] == "d_active"
    assert (
        left.why_not(d_alive=False, e_active=True, arm_held=True)["code"] == "e_active"
    )
    assert (
        left.why_not(d_alive=False, e_active=False, arm_held=True)["code"]
        == "arm_in_use"
    )
    assert (
        left.why_not(d_alive=False, e_active=False, arm_held=False, gpu_held=True)[
            "code"
        ]
        == "gpu_in_use"
    )


def test_an_obstacle_that_cannot_be_read_withholds_the_stop():
    for name in ("d_alive", "e_active", "arm_held", "gpu_held"):
        args = {
            "d_alive": False,
            "e_active": False,
            "arm_held": False,
            "gpu_held": False,
        }
        args[name] = None
        got = left.why_not(**args)
        assert got["code"] == "unreadable", name
    # and a known obstacle outranks an unknown one
    got = left.why_not(d_alive=None, e_active=True, arm_held=False)
    assert got["code"] == "e_active"


def test_the_gpu_lock_is_not_asked_unless_the_caller_does():
    assert left.why_not(d_alive=False, e_active=False, arm_held=False) is None


def test_a_reason_becomes_the_matching_refusal():
    left.refuse_for(None)
    for code in ("d_active", "e_active", "arm_in_use", "gpu_in_use"):
        with pytest.raises(Refuse) as caught:
            left.refuse_for({"code": code, "text": ""})
        assert caught.value.code == code and caught.value.status == 409
    with pytest.raises(Refuse) as caught:
        left.refuse_for({"code": "unreadable", "text": ""})
    assert caught.value.code == "state_unreadable"


# --- stopping units ------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("systemctl") is None, reason="no systemctl")
def test_systemctl_refusing_a_unit_that_is_not_loaded_is_reported_as_it_is():
    """The real command, on a name that cannot exist: it exits non-zero and says why."""
    name = "levi-aeri-vlm-a2test-no-such-unit-0001.service"
    done = subprocess.run(
        ["systemctl", "--user", "show", name, "-p", "LoadState"],
        capture_output=True,
        text=True,
        check=False,
    )
    if "not-found" not in done.stdout:
        pytest.skip("this machine's systemd answers differently")
    got = left.stop_units([name])
    assert len(got) == 1 and got[0]["name"] == name
    # systemd stops a unit that is not loaded without complaint on some versions:
    # either way the answer is well formed and holds no path.
    assert isinstance(got[0]["ok"], bool)
    assert got[0]["detail"] is None or "/" not in got[0]["detail"].replace("<path>", "")


class Runner:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        result = self.results[len(self.calls) - 1]
        if isinstance(result, BaseException):
            raise result
        return result


def ran(rc=0, out="", err=""):
    return aeri_prep.Ran(rc, out, err)


def test_each_unit_is_stopped_in_turn_and_a_failure_does_not_stop_the_loop():
    run = Runner(
        [
            ran(0),
            ran(
                5,
                "",
                "Failed to stop levi-aeri-vlm-8100.service: Unit not loaded at /run/user/1000/x.",
            ),
            subprocess.TimeoutExpired("systemctl", 90),
            FileNotFoundError("systemctl"),
            ran(0),
        ]
    )
    names = [f"levi-aeri-vlm-{n}.service" for n in range(5)]
    got = left.stop_units(names, run)
    assert [g["name"] for g in got] == names
    assert [g["ok"] for g in got] == [True, False, False, False, True]
    assert got[0]["detail"] is None
    assert "Failed to stop" in got[1]["detail"] and "/run/user" not in got[1]["detail"]
    assert got[2]["detail"] == "timeout"
    assert got[3]["detail"] == "FileNotFoundError"
    for argv, kw in run.calls:
        assert (
            argv[:3] == ["systemctl", "--user", "stop"]
            and kw["timeout"] == left.STOP_TIMEOUT_S
        )
    assert not [a for argv, _ in run.calls for a in argv if a == "--force"]


def test_a_failure_with_no_text_still_says_something():
    got = left.stop_units(["levi-aeri-vlm-1.service"], Runner([ran(3)]))
    assert got[0]["detail"] == "exit 3"


# --- stopping the live service ---------------------------------------------------------------


SECRETS = {
    "LEVI_UI_TOKEN": "secret-ui-token",
    "HF_TOKEN": "secret-hf",
    "LEVI_WORKSPACE": "/somewhere",
    "ANTHROPIC_API_KEY": "secret-key",
    "HOME": "/home/x",
    "USER": "x",
    "LANG": "C.UTF-8",
    "XDG_RUNTIME_DIR": "/run/user/1000",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
    "PATH": "/evil/bin",
}


def test_the_environment_is_an_allowlist():
    env = left.live_env(SECRETS)
    assert set(env) == {
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    }
    assert env["PATH"] == left.LIVE_ENV_PATH, "our PATH, not the caller's"
    assert left.live_env({"HOME": ""}) == {"PATH": left.LIVE_ENV_PATH}


def test_the_live_service_is_stopped_through_its_unit_with_an_allowlisted_environment(
    world,
):
    world.set_live_stop(say="")
    got = left.stop_live(world.site.live.unit, SECRETS, world.systemctl)
    assert got == {"returncode": 0, "tail": ""}
    seen = world.seen_live_stop()
    assert seen["argv"] == ["systemctl", "--user", "stop", "levi-live.service"]
    assert "--force" not in seen["argv"]
    assert seen["env"] == left.live_env(SECRETS)
    assert "LEVI_UI_TOKEN" not in seen["env"] and "HF_TOKEN" not in seen["env"]
    assert (
        "ANTHROPIC_API_KEY" not in seen["env"] and "LEVI_WORKSPACE" not in seen["env"]
    )
    assert seen["env"]["PATH"] == left.LIVE_ENV_PATH
    assert seen["env"]["HOME"] == "/home/x"


def test_the_decision_is_the_unit_state_read_after_not_the_exit_code(world):
    """``stop_live`` only reports what the command said; the card's re-read of
    the unit decides (``aeri_api``): a refusing command leaves it running."""
    world.set_live_stop(stops=False, rc=1, say="Job for levi-live.service canceled.")
    got = left.stop_live(world.site.live.unit, {}, world.systemctl)
    assert got == {"returncode": 1, "tail": "Job for levi-live.service canceled."}
    assert json.loads(world.live_file.read_text())["active"] == "active", (
        "nothing else was tried"
    )


def test_the_tail_is_bounded_and_scrubbed(world):
    world.set_live_stop(say="x" * 2000 + " see /home/marvel/secret/file")
    got = left.stop_live(world.site.live.unit, {}, world.systemctl)
    assert len(got["tail"]) <= left.TAIL_CHARS and "/home/marvel" not in got["tail"]


def test_a_command_that_hangs_or_is_missing_is_a_stop_failure(world):
    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    with pytest.raises(Refuse) as caught:
        left.stop_live("levi-live.service", {}, slow)
    assert (caught.value.code, caught.value.extra["reason"]) == (
        "live_stop_failed",
        "timeout",
    )

    def gone(argv, **kw):
        raise FileNotFoundError(argv[0])

    with pytest.raises(Refuse) as caught:
        left.stop_live("levi-live.service", {}, gone)
    assert caught.value.extra["reason"] == "not_run"


def test_the_timeout_covers_the_units_own_stop_job(world):
    """The unit says ``TimeoutStopSec=180``; ``systemctl stop`` waits for the stop
    job, so the call waits longer (the product's own stop waits 210 s)."""
    from levi.live import service_control

    seen = {}

    def spy(argv, **kw):
        seen.update(kw)
        return ran(0)

    left.stop_live("levi-live.service", {"HOME": "/h"}, spy)
    assert seen["timeout"] == left.LIVE_STOP_TIMEOUT_S == 210.0
    assert left.LIVE_STOP_TIMEOUT_S == service_control.STOP_TIMEOUT_S
    # the documented unit (docs/SUPERVISION.md), not a file of this machine
    text = (Path(__file__).resolve().parents[2] / "docs" / "SUPERVISION.md").read_text()
    assert "TimeoutStopSec=180" in text and left.LIVE_STOP_TIMEOUT_S > 180


@pytest.mark.parametrize(
    "unit",
    [
        "levi-live.service",
        "levi-live-cold.service",
        "levi-live@x.service",
    ],
)
def test_a_live_unit_name_is_stopped(unit):
    seen = []

    def spy(argv, **kw):
        seen.append(argv)
        return ran(0)

    left.stop_live(unit, {}, spy)
    assert seen == [["systemctl", "--user", "stop", unit]]


@pytest.mark.parametrize(
    "unit",
    [
        "levi-product.service",
        "levi-live-levi-product.service",
        "levi-aeri-vlm-8100.service",
        "levi-aeri-policy-forward.service",
        "ssh.service",
        "levi-live",
        "levi-live.scope",
        "-all",
        "levi-live.service --now",
        "",
        None,
        7,
    ],
)
def test_any_other_unit_is_never_stopped_whatever_the_site_file_says(unit):
    def never(argv, **kw):
        raise AssertionError("a command ran")

    with pytest.raises(Refuse) as caught:
        left.stop_live(unit, {}, never)
    assert (caught.value.code, caught.value.extra["reason"]) == (
        "live_stop_failed",
        "unit_invalid",
    )


def test_the_unit_pattern_is_the_one_the_products_own_stop_accepts():
    from levi.live import service_control

    assert service_control.UNIT_NAME.pattern == "^" + left.LIVE_UNIT.pattern + "$"
    assert service_control.FORBIDDEN_IN_UNIT == left.LIVE_UNIT_FORBIDDEN
