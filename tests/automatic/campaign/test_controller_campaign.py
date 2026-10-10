# ruff: noqa: F401, F811
"""The campaign controller process (T-API-2): its own systemd unit, a restart
after a crash through ``attach``, one campaign per robot, requests that
survive a controller restart. The unit tool is never run (the campaign guard
refuses it); the one function that would start a unit is replaced."""

import json
import shutil
import threading

from test_api_campaign_common import (
    URL,
    aeri_home_fixture,
    guard_fixture,
    wait_for,
    world,
)
from test_api_campaign_dry import at_segment, keys_of, run_to_end, started

from levi.automatic.campaign import adapters as A
from levi.automatic.campaign import controller as C
from levi.automatic.campaign import journal as J


def test_the_controller_gets_its_own_unit_and_none_runs_in_the_product(
    world, aeri_home, monkeypatch
):
    monkeypatch.setattr(C, "CONTROLLER_BACKEND", "systemd")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/systemd-run")
    calls = []
    monkeypatch.setattr(C, "start_unit", calls.append)
    cid, _ = started(world)
    (argv,) = calls
    assert argv[:3] == [
        "/usr/bin/systemd-run",
        "--user",
        f"--unit=levi-aeri-campaign-{cid}",
    ]
    assert "--collect" in argv and "KillMode=control-group" in argv
    assert not any("Restart" in a for a in argv)
    assert argv[-4:-2] == ["-m", "levi.automatic.campaign.controller"]
    assert argv[-2:] == ["--campaign-id", cid]
    assert f"--setenv=LEVI_AERI_HOME={aeri_home}" in argv
    # Nothing was started here: no controller, no journal yet.
    snap = world.snapshot(cid)
    assert snap["state"] == "DRAFT" and snap["controller"] == {"alive": False}
    assert not (aeri_home / "campaigns" / cid / "journal.jsonl").exists()
    # What the unit would run: the same entry point.
    stop = threading.Event()
    thread = threading.Thread(
        target=C.serve, args=(cid,), kwargs={"stop": stop, "poll_s": 0.02}, daemon=True
    )
    thread.start()
    try:
        at_segment(world, cid, 1)
    finally:
        stop.set()
        thread.join(10)


def test_a_failed_unit_start_leaves_no_half_started_campaign(
    world, aeri_home, monkeypatch
):
    monkeypatch.setattr(C, "CONTROLLER_BACKEND", "systemd")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    job = world.make_job()
    answer, _ = world.start(job, execution_mode="dry_run")
    assert answer.status_code == 503
    assert answer.json()["detail"]["code"] == "systemd_unavailable"
    assert not (aeri_home / "campaigns").exists() or not any(
        (aeri_home / "campaigns").iterdir()
    )
    # The same request works once the unit tool is there.
    monkeypatch.setattr(C, "CONTROLLER_BACKEND", "inprocess")
    again, _ = world.start(job, execution_mode="dry_run")
    assert again.status_code == 202, again.text


def test_a_controller_that_died_is_brought_back_with_attach(world):
    cid, _ = started(world)
    snap = at_segment(world, cid, 1)
    C.stop_all()
    wait_for(lambda: world.snapshot(cid)["controller"]["alive"] is False)
    # Nothing can be answered while nobody listens.
    down = world.confirm(cid, snap["todo"], "late-1")
    assert (
        down.status_code == 409 and down.json()["detail"]["code"] == "controller_down"
    )
    assert world.command(cid, "pause", "late-2").status_code == 409
    attach = world.client.post(f"{URL}/{cid}/attach", json={"request_id": "att-1"})
    assert attach.status_code == 202, attach.text
    # The campaign was asking for the scene: it asks again, then goes on.
    todo = wait_for(
        lambda: (
            (s := world.snapshot(cid))["controller"]["alive"]
            and s["todo"]
            and s["todo"]["kind"] == "place_cards"
            and s["todo"]
        )
    )
    assert todo["challenge"] != snap["todo"]["challenge"]
    again = world.client.post(f"{URL}/{cid}/attach", json={"request_id": "att-2"})
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "controller_alive"
    assert world.confirm(cid, todo, "env-1").json()["result"] == "applied"
    run_to_end(world, cid, start=2)
    ended = world.client.post(f"{URL}/{cid}/attach", json={"request_id": "att-3"})
    assert ended.status_code == 409
    assert ended.json()["detail"]["code"] == "campaign_ended"
    gone = world.client.post(f"{URL}/c-nope/attach", json={"request_id": "att-4"})
    assert gone.status_code == 404


class Crash(BaseException):
    """What a power cut looks like to the controller: nothing catches it."""


def test_a_crash_in_the_middle_of_a_launch_is_never_launched_again_by_itself(
    world, monkeypatch
):
    cid, _ = started(world)
    snap = at_segment(world, cid, 1)
    real = A.DryRunLauncher.launch

    def crash(self, *args, **kwargs):
        raise Crash

    monkeypatch.setattr(A.DryRunLauncher, "launch", crash)
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    world.confirm(cid, snap["todo"], "env-1")
    wait_for(lambda: world.snapshot(cid)["controller"]["alive"] is False)
    monkeypatch.setattr(A.DryRunLauncher, "launch", real)
    world.client.post(f"{URL}/{cid}/attach", json={"request_id": "att-1"})
    locked = world.wait_state(cid, "FAULT_LOCKED")
    assert locked["wait_reason"] == "recovery_ambiguous"
    assert locked["safety"]["fused"] is True
    assert locked["todo"]["kind"] == "recover_run"
    assert locked["todo"]["reason"] == "recovery_ambiguous"
    # Nothing launches by itself; only the person's answer leads on.
    answer = world.confirm(
        cid, locked["todo"], "rec-1", kind="env", options={"relaunch": True}
    )
    assert answer.json()["result"] == "applied"
    again = wait_for(
        lambda: (
            (s := world.snapshot(cid))["state"] == "ENV_CONFIRM"
            and s["todo"]
            and s["todo"]["kind"] == "place_cards"
            and s["todo"]
        )
    )
    assert world.confirm(cid, again, "env-2").json()["result"] == "applied"
    run_to_end(world, cid, start=2)


def test_one_campaign_holds_the_robot_at_a_time(world):
    cid, _ = started(world)
    at_segment(world, cid, 1)
    job = world.make_job(name="second")
    answer, _ = world.start(job, request_id="req-second", execution_mode="dry_run")
    assert answer.status_code == 409
    assert answer.json()["detail"]["code"] == "robot_busy"
    run_to_end(world, cid)
    wait_for(lambda: world.snapshot(cid)["controller"]["alive"] is False)
    answer, _ = world.start(job, request_id="req-second", execution_mode="dry_run")
    assert answer.status_code == 202, answer.text


def test_a_peek_asked_while_nobody_listens_is_journaled_later(world, aeri_home):
    cid, _ = started(world)
    at_segment(world, cid, 1)
    C.stop_all()
    wait_for(lambda: world.snapshot(cid)["controller"]["alive"] is False)
    answer = world.command(cid, "unblind", "ub-1")
    assert answer.json()["result"] == "applied"
    # It already counts for what the page may show ...
    assert world.snapshot(cid)["blinded"] is False
    # ... and the controller journals it as soon as it is back.
    world.client.post(f"{URL}/{cid}/attach", json={"request_id": "att-1"})
    wait_for(lambda: world.snapshot(cid)["peeks"] == 1)
    notes = [
        json.loads(line)
        for line in (aeri_home / "campaigns" / cid / "journal.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [n["note"]["code"] for n in notes if n["record"] == "note"].count(
        "unblind_peek"
    ) == 1


def test_the_backend_opens_no_connection_and_starts_no_tool(world, campaign_guard):
    cid, _ = started(world)
    run_to_end(world, cid)
    assert campaign_guard.connects == []
    assert campaign_guard.commands == []


def test_the_journal_has_the_controller_as_its_only_writer(world, aeri_home):
    cid, _ = started(world)
    snap = at_segment(world, cid, 1)
    folder = aeri_home / "campaigns" / cid
    before = (folder / "journal.jsonl").read_bytes()
    for _ in range(5):
        world.snapshot(cid)
        world.client.get(URL)
    assert (folder / "journal.jsonl").read_bytes() == before
    # Requests are files; the journal changes only in the controller.
    world.confirm(cid, snap["todo"], "env-1")
    wait_for(lambda: (folder / "journal.jsonl").read_bytes() != before)
    run_to_end(world, cid, start=2)


def test_ids_are_matched_strictly_before_they_touch_a_path(world, aeri_home):
    cid, _ = started(world)
    snap = at_segment(world, cid, 1)
    for bad in ("../x", "a/b", "", "x" * 200, "a b", "é"):
        for url, body in (
            (
                f"{URL}/{cid}/confirm",
                {
                    "command_id": bad,
                    "kind": "env",
                    "challenge": snap["todo"]["challenge"],
                },
            ),
            (f"{URL}/{cid}/pause", {"command_id": bad, "confirm": "pause"}),
            (f"{URL}/{cid}/unblind", {"command_id": bad, "confirm": "unblind"}),
            (f"{URL}/{cid}/attach", {"request_id": bad}),
        ):
            assert world.client.post(url, json=body).status_code == 422, (url, bad)
    ctl = aeri_home / "campaigns" / cid / "ctl"
    assert [p.name for p in (ctl / "answers").iterdir()] == []
    assert world.snapshot(cid)["todo"]["challenge"]
