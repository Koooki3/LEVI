"""The control rate on the hosted run: the client the page starts is the very
command the form renders (``levi/marvel0/run.py`` calls ``command.validate`` and
``command.render`` on the same form body), so the rate, the scaled per-step
caps and the warnings reach the launch, the start checks and the start
challenge, and a rate the form refuses is refused before anything is started.

The client is scripted (``Machine`` in ``test_marvel0_run.py``); nothing is
started and nothing is signalled."""

import json

import pytest
from test_marvel0_run import (
    GROUP,
    SFT,
    make_machine,  # noqa: F401 - the fixture
    refusal,
)

from levi.marvel0 import command


def launched_flags(m):
    spec = json.loads(m.log_path().with_suffix(".spec.json").read_text())
    return spec["argv"][2:]


def test_a_start_at_10_hz_launches_without_a_rate_flag(machine):
    m = machine
    m.start()
    flags = launched_flags(m)
    assert not [f for f in flags if f.startswith("--control-hz")]
    assert "--max-step-m=0.1" in flags and "--max-step-rad=0.35" in flags


@pytest.mark.parametrize(
    "hz, cap_m, cap_rad",
    [
        (5, "0.1", "0.35"),
        (20, "0.05", "0.175"),
        (25, "0.04", "0.14"),
        (50, "0.02", "0.07"),
    ],
)
def test_a_start_at_another_rate_launches_the_rate_and_the_scaled_caps(
    machine, hz, cap_m, cap_rad
):
    m = machine
    m.start(control_hz=hz)
    flags = launched_flags(m)
    assert flags.count(f"--control-hz={hz}") == 1
    assert f"--max-step-m={cap_m}" in flags
    assert f"--max-step-rad={cap_rad}" in flags
    assert flags[-1] == "--no-display-images"
    # the launched command is the one the form renders, plus the hosted switch
    site = m.site
    mdl = m.sup.catalog.get(site, SFT)
    form = command.validate(site, mdl, m.form(control_hz=hz))
    assert flags == [
        *command.render(site, mdl, form).argv[len(command.PREFIX) :],
        "--no-display-images",
    ]


def test_the_start_challenge_carries_the_warnings_of_the_rate(machine):
    m = machine
    quiet = m.sup.challenge({"purpose": "start", "form": m.form(min_z=0.075)})
    assert quiet["warnings"] == []
    fast = m.sup.challenge(
        {"purpose": "start", "form": m.form(min_z=0.075, control_hz=25, max_steps=900)}
    )
    assert [w["code"] for w in fast["warnings"]] == [
        "control_hz_not_trained",
        "control_hz_untested",
    ]
    assert fast["group"] == GROUP


def test_a_rate_is_part_of_the_form_the_challenge_is_bound_to(machine):
    """A token made for 20 Hz does not start a client at 50 Hz."""
    m = machine
    c = m.sup.challenge({"purpose": "start", "form": m.form(control_hz=20)})
    body = {
        "request_id": m.rid(),
        "form": m.form(control_hz=50),
        "challenge": c["challenge"],
        "confirm": c["confirm"],
        "attest": {"estop": True, "scene_reset": True, "area_clear": True},
    }
    assert refusal(m.sup.start, body)[:2] == (409, "challenge_stale")
    assert m.spawned == []


@pytest.mark.parametrize("bad", [15, 0, -5, 100])
def test_a_rate_the_form_refuses_is_refused_with_its_own_code_and_starts_nothing(
    machine, bad
):
    m = machine
    status, code, extra = refusal(
        m.sup.challenge, {"purpose": "start", "form": m.form(control_hz=bad)}
    )
    assert (status, code) == (422, "control_hz_unsupported")
    assert extra["field"] == "control_hz" and extra["options"] == [5, 10, 20, 25, 50]
    assert refusal(m.start, control_hz=bad)[:2] == (422, "control_hz_unsupported")
    assert m.spawned == []


@pytest.mark.parametrize("bad", ["20", 20.0, True, "10; rm -rf /"])
def test_a_rate_of_another_type_is_refused_as_invalid_type(machine, bad):
    m = machine
    status, code, extra = refusal(m.start, control_hz=bad)
    assert (status, code) == (422, "invalid_type") and extra["field"] == "control_hz"
    assert m.spawned == []
