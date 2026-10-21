"""``src/components/marvel0/__tests__/marvel0-stack-act.samples.json``: the
committed answers of the arm stack's routes that the frontend tests parse with
the page's own readers. They are what the backend gives on a fully fake marvel0
(``regen_stack_act_sample.py``), so a field the backend renames, drops or adds
fails here, naming the command that rewrites the file, instead of leaving the
page reading a shape that no longer exists."""

import json
import re
import tempfile
import time

from regen_sample import ROOT
from regen_stack_act_sample import SAMPLE, build
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard

REGENERATE = "uv run --no-sync python tests/marvel0/regen_stack_act_sample.py"
PATH_IN_TEXT = re.compile(r"(?<![\w.:-])/(?:[A-Za-z0-9_.~+@%=,-]+/)+")


def test_the_committed_samples_are_what_the_backend_builds():
    name = SAMPLE.relative_to(ROOT)
    assert SAMPLE.is_file(), f"{name} is missing; create it with: {REGENERATE}"
    assert SAMPLE.read_text() == build(), (
        f"{name} differs from the answers the backend builds; "
        f"regenerate it with: {REGENERATE}"
    )


def test_the_build_does_not_depend_on_the_checkouts_path(monkeypatch, tmp_path):
    expected = build()
    long_tmp = tmp_path / ("x" * 37) / "deeper"
    long_tmp.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(long_tmp))
    monkeypatch.setattr(tempfile, "tempdir", None)  # read TMPDIR again
    assert build() == expected


def test_the_build_does_not_depend_on_the_machines_time_zone(monkeypatch):
    expected = build()
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        assert build() == expected
    finally:
        monkeypatch.undo()
        time.tzset()


def samples():
    return json.loads(SAMPLE.read_text())


def test_the_samples_name_every_state_the_page_reads():
    views = samples()["views"]

    def states(name):
        t = views[name]["act"]["terminals"]
        return {k: t[k]["state"] for k in "mabc"}

    assert states("idle") == {"m": "idle", "a": "idle", "b": "unknown", "c": "idle"}
    assert states("monitor_ready")["m"] == "ready"
    assert states("a_starting")["a"] == "starting"
    assert states("a_ready")["a"] == "ready"
    assert states("a_failed")["a"] == "failed"
    assert states("b_starting")["b"] == "starting"
    assert states("b_ready")["b"] == "ready"
    assert states("c_starting")["c"] == "starting"
    assert states("c_ready") == {"m": "ready", "a": "ready", "b": "ready", "c": "ready"}
    assert states("c_stopped_arm_held")["c"] == "idle"
    seq = views["sequence_waiting_b"]["act"]["sequence"]
    assert (seq["state"], seq["next"]) == ("waiting_confirm", "b")
    assert views["sequence_running"]["act"]["sequence"]["step"] == "monitor"
    assert views["control_disabled"]["act"]["enabled"] is False


def test_the_samples_name_the_states_of_the_second_round():
    views = samples()["views"]
    b = views["b_check_old"]["act"]["terminals"]["b"]
    assert (b["state"], b["reason"], b["can_start"]) == (
        "unknown",
        "b.probe_stale",
        True,
    )
    assert [(x["code"], x["level"]) for x in b["start_blockers"]] == [
        ("b_probe_stale", "warn")
    ]
    assert views["b_check_made"]["act"]["terminals"]["b"]["state"] == "idle"
    assert views["b_check_old"]["act"]["checks"]["can_probe_b"] is True
    by_button = views["b_check_by_button"]["act"]
    assert by_button["terminals"]["b"]["state"] == "idle"
    assert by_button["operation"]["kind"] == "probe_b"
    assert by_button["checks"]["b_probe"]["state"] == "absent"
    assert samples()["answers"]["probe_b"] == {
        "done": "probe_started",
        "kind": "probe_b",
    }
    assert samples()["refusals"]["probe_b_with_unit"]["detail"]["code"] == (
        "gate.b_unit_absent"
    )
    a = views["a_check_sockets_draining"]["act"]["terminals"]["a"]
    assert (a["state"], a["reason"]) == ("idle", "a.sockets_draining")
    t = views["c_health_lost"]["act"]["terminals"]
    assert (t["c"]["state"], t["c"]["reason"]) == ("unknown", "c.health_lost")
    assert "stop_c_first" in [x["code"] for x in t["a"]["stop_blockers"]]
    refusal = samples()["refusals"]["start_b_check_old"]
    assert refusal["detail"]["code"] == "b_probe_started"


def test_a_starting_unit_lists_what_it_has_shown_and_what_is_missing():
    a = samples()["views"]["a_starting"]["act"]["terminals"]["a"]
    assert [x["key"] for x in a["readiness"]["seen"]] == ["fifo"]
    assert [x["key"] for x in a["readiness"]["missing"]] == [
        "spawners",
        "health_hardware_active",
    ]
    assert a["readiness"]["waited_s"] == 12.0


def test_the_gripper_test_has_a_success_and_a_failure():
    views = samples()["views"]
    ok = views["gripper_success"]["act"]["gripper_test"]["last"]
    bad = views["gripper_failed"]["act"]["gripper_test"]["last"]
    assert (ok["ok"], ok["code"]) == (True, "reached")
    assert (bad["ok"], bad["code"], bad["stalled"]) == (False, "stalled", True)


def test_a_refused_start_lists_its_blockers():
    a = samples()["views"]["idle"]["act"]["terminals"]["a"]
    assert a["can_start"] is False
    codes = [b["code"] for b in a["start_blockers"]]
    assert "gate.monitor_fresh" in codes and "gate.fci_probe" in codes
    assert all(
        set(b) == {"code", "level", "text", "params"} for b in a["start_blockers"]
    )


def test_the_samples_hold_the_refusals_a_page_has_to_word():
    refusals = samples()["refusals"]
    assert {k: v["status"] for k, v in refusals.items()} == {
        "start_a_blocked": 409,
        "start_a_without_attest": 400,
        "start_c_before_b": 409,
        "start_b_check_old": 409,
        "probe_b_with_unit": 409,
        "stop_c_with_client": 409,
        "control_disabled": 403,
    }
    for item in refusals.values():
        assert set(item["detail"]) >= {"code", "message"}


def test_the_samples_hold_no_path_but_the_templates():
    text = json.dumps(samples())
    found = set(PATH_IN_TEXT.findall(text))
    assert all(p.startswith("/path/to/") for p in found), found
    assert "10.42.0.157" not in text
