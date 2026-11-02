"""Every path that starts a client gives the cameras back first: the hosted
evaluation run (``run.Supervisor.start``, which the page's change-of-instruction
restart calls again) and the hosted AERI terminal D (``aeri_run.Supervisor.start``),
on the scripted machines of their own tests and a preview whose worker is a
scripted process. A worker that does not exit refuses the start with a reason
instead of letting the client fail on a busy camera; no preview starts while a
launch is under way; a start that is refused before it reaches the launch does
not cost the preview its cameras."""

import signal

import pytest
from aeri_machine import AeriMachine
from test_marvel0_aeri_run import FAST
from test_marvel0_preview import DeploySup, Rig, RunSup, status
from test_marvel0_run import ATTEST, Machine

from levi.marvel0 import (
    aeri_run,
    aeri_run_api,
    deploy_api,
    preview,
    record,
    run,
    run_api,
)
from levi.marvel0 import aeri_state as st
from levi.marvel0.aeri_state import Refuse as AeriRefuse


@pytest.fixture
def machine(tmp_path, monkeypatch):
    m = Machine(tmp_path / "run", monkeypatch)
    yield m
    m.close()


@pytest.fixture
def aeri(tmp_path, monkeypatch):
    for key, value in FAST:
        monkeypatch.setattr(st, key, value)
    m = AeriMachine(tmp_path / "aeri", monkeypatch)
    m.prepare(rounds=5, steps=200)
    yield m
    m.close()


def preview_rig(tmp_path, monkeypatch, **kw):
    """A preview over a scripted worker, as the process's one."""
    folder = tmp_path / "preview"
    folder.mkdir()
    rig = Rig(folder, **kw)
    monkeypatch.setattr(preview, "manager", lambda: rig.mgr)
    return rig


def watch_launch(host, rig, events):
    """Note in ``events`` what the preview's worker looked like at the moment the
    client was launched."""
    original = host.spawn

    def spawn(*args, **kwargs):
        proc = rig.procs[0] if rig.procs else None
        events.append(("launch", None if proc is None else proc.code))
        return original(*args, **kwargs)

    host.spawn = spawn


# --- the hosted evaluation run --------------------------------------------------------------


def test_the_hosted_run_start_stops_the_preview_and_waits_before_it_launches(
    tmp_path, monkeypatch, machine
):
    rig = preview_rig(tmp_path, monkeypatch)
    rig.running()
    events = []
    watch_launch(machine.host, rig, events)
    answer = machine.start()
    assert answer["created"] is True and len(machine.spawned) == 1
    # the worker had exited (code 0 after SIGTERM) when the client was launched
    assert events == [("launch", 0)]
    assert rig.signals[0] == (rig.procs[0].pid, signal.SIGTERM)
    assert rig.mgr.view()["stopped_by"] == "client_start"
    assert rig.mgr._held == 0  # the block is over; the run itself blocks a preview


def test_the_default_hand_back_of_the_host_is_the_previews(machine):
    assert machine.host.camera_handback is preview.handback
    assert aeri_run.Host().camera_handback is preview.handback


def test_a_worker_that_does_not_exit_refuses_the_start_and_launches_nothing(
    tmp_path, monkeypatch, machine
):
    rig = preview_rig(tmp_path, monkeypatch, ends_on=None)
    rig.running()
    with pytest.raises(run.Refuse) as caught:
        machine.start()
    exc = caught.value
    assert (exc.status, exc.code) == (409, "camera_preview_busy")
    assert "camera preview" in exc.message
    assert machine.spawned == [] and machine.sup.active() is None
    assert rig.mgr._held == 0
    # the route's answer is the standard one
    detail = run.http(exc).detail
    assert detail["code"] == "camera_preview_busy"
    # once the worker is gone, the same form starts
    rig.procs[0].code = -9
    rig.mgr.tick()
    assert machine.start()["created"] is True


def test_no_preview_starts_while_the_client_is_being_launched(
    tmp_path, monkeypatch, machine
):
    rig = preview_rig(tmp_path, monkeypatch)
    answers = []
    original = machine.host.spawn

    def spawn(*args, **kwargs):
        with pytest.raises(preview.Refused) as caught:
            rig.mgr.start()
        answers.append(caught.value.code)
        return original(*args, **kwargs)

    machine.host.spawn = spawn
    machine.start()
    assert answers == ["client_starting"]
    assert rig.procs == []


def test_a_start_refused_before_the_launch_leaves_the_preview_its_cameras(
    tmp_path, monkeypatch, machine
):
    rig = preview_rig(tmp_path, monkeypatch)
    rig.running()
    form = machine.form()
    with pytest.raises(run.Refuse) as caught:
        machine.sup.start(
            {
                "request_id": machine.rid(),
                "form": form,
                "challenge": "not-a-token",
                "confirm": run.PHRASES["start"].format(group=run._group_of(form)),
                "attest": dict(ATTEST),
            }
        )
    assert caught.value.code.startswith("challenge_")
    assert rig.mgr.view()["state"] == "on" and rig.signals == []


def test_while_the_hosted_run_is_active_a_preview_is_refused_by_the_real_rule(
    tmp_path, monkeypatch, machine
):
    rig = preview_rig(tmp_path, monkeypatch)
    monkeypatch.setattr(run_api, "supervisor", lambda: machine.sup)
    monkeypatch.setattr(aeri_run_api, "supervisor", lambda: RunSup())
    monkeypatch.setattr(deploy_api, "supervisor", lambda: DeploySup(status()))
    rig.host.blockers = preview.blockers
    assert rig.mgr.view()["may_start"] is True
    machine.start()
    with pytest.raises(preview.Refused) as caught:
        rig.mgr.start()
    assert caught.value.code == "run_active"
    assert rig.procs == []


def test_a_hosted_run_record_that_cannot_be_read_refuses_a_preview(
    tmp_path, monkeypatch, machine
):
    """After a restart the supervisor takes a record it cannot read for "no
    run" (``adopt``); the preview reads the record itself and fails closed."""
    avoid = (
        machine.site.rollout_root,
        machine.site.checkpoints_root,
        machine.site.openpi_checkout,
    )
    directory = run.state_dir(avoid, machine.workspace)
    (directory / record.RECORD).write_text("{not json")
    sup = run.Supervisor(
        machine.host,
        site_loader=lambda: machine.site,
        workspace=machine.workspace,
    )
    sup.adopt()
    assert sup.active() is None  # (what the supervisor itself reports)
    monkeypatch.setattr(run_api, "supervisor", lambda: sup)
    monkeypatch.setattr(aeri_run_api, "supervisor", lambda: RunSup())
    monkeypatch.setattr(deploy_api, "supervisor", lambda: DeploySup(status()))
    assert preview.blockers() == ["state_unreadable"]
    rig = preview_rig(tmp_path, monkeypatch)
    rig.host.blockers = preview.blockers
    with pytest.raises(preview.Refused) as caught:
        rig.mgr.start()
    assert caught.value.code == "state_unreadable" and rig.procs == []
    (directory / record.RECORD).unlink()  # no record at all is "no run"
    assert preview.blockers() == []


def _supervisor_that_could_not_adopt(machine):
    """A supervisor over the machine's workspace whose first read of the site
    failed, so ``adopt`` took the record for "no run" and never looks again (a
    restart of the service that finds a hosted run on disk)."""
    calls: list = []

    def site_loader():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("the site file could not be read just then")
        return machine.site

    sup = run.Supervisor(
        machine.host, site_loader=site_loader, workspace=machine.workspace
    )
    sup.adopt()
    assert sup.active() is None  # (what the supervisor itself reports)
    return sup


def _blockers_over(monkeypatch, sup):
    monkeypatch.setattr(run_api, "supervisor", lambda: sup)
    monkeypatch.setattr(aeri_run_api, "supervisor", lambda: RunSup())
    monkeypatch.setattr(deploy_api, "supervisor", lambda: DeploySup(status()))
    return preview.blockers()


def test_a_started_hosted_run_the_supervisor_lost_sight_of_is_still_a_run(
    tmp_path, monkeypatch, machine
):
    machine.start()  # the record on disk: started, its launcher alive
    sup = _supervisor_that_could_not_adopt(machine)
    assert machine.child.poll() is None
    assert _blockers_over(monkeypatch, sup) == ["run_active"]
    rig = preview_rig(tmp_path, monkeypatch)
    rig.host.blockers = preview.blockers
    with pytest.raises(preview.Refused) as caught:
        rig.mgr.start()
    assert caught.value.code == "run_active" and rig.procs == []


def test_a_started_record_whose_launcher_is_gone_is_never_taken_for_no_run(
    monkeypatch, machine
):
    machine.start()
    sup = _supervisor_that_could_not_adopt(machine)
    machine.child.exits(0)  # nobody settled the record: the launcher has gone
    found = _blockers_over(monkeypatch, sup)
    assert found == ["state_unreadable"]  # a refusal that says what is known
    # an identity that cannot be checked is the same
    monkeypatch.setattr(
        machine.host, "identity", lambda pid: (_ for _ in ()).throw(OSError("proc"))
    )
    assert _blockers_over(monkeypatch, sup) == ["state_unreadable"]


def test_an_ended_record_is_no_run(monkeypatch, machine):
    machine.start()
    machine.child.exits(0)
    machine.sup.adopt()
    machine.sup.view()  # settles the dead launcher: the record says ended
    sup = _supervisor_that_could_not_adopt(machine)
    assert _blockers_over(monkeypatch, sup) == []


def test_every_start_hands_back_again_the_restart_for_a_new_instruction_included(
    tmp_path, monkeypatch, machine
):
    """The change of instruction posts ``/run/start`` again: a start that failed
    and the one after it each take the cameras back from a preview opened in
    between."""
    rig = preview_rig(tmp_path, monkeypatch)
    rig.running()
    original = machine.host.spawn

    def broken(argv, **kw):
        raise OSError("no such launcher")

    machine.host.spawn = broken
    with pytest.raises(run.Refuse) as caught:
        machine.start()
    assert caught.value.code == "start_failed"
    assert rig.procs[0].code == 0  # the failed launch had still taken them back
    machine.host.spawn = original
    rig.running()  # the page opens a preview again
    assert rig.procs[1].code is None
    again = machine.start()
    assert again["created"] is True
    assert rig.procs[1].code == 0 and rig.mgr._held == 0


# --- the hosted AERI terminal D -------------------------------------------------------------


def test_the_aeri_start_stops_the_preview_and_waits_before_it_launches(
    tmp_path, monkeypatch, aeri
):
    rig = preview_rig(tmp_path, monkeypatch)
    rig.running()
    events = []
    watch_launch(aeri.host, rig, events)
    answer = aeri.start()
    assert answer["state"] == "launching" and len(aeri.spawned) == 1
    assert events == [("launch", 0)]
    assert rig.mgr.view()["stopped_by"] == "client_start" and rig.mgr._held == 0


def test_a_worker_that_does_not_exit_refuses_the_aeri_start(
    tmp_path, monkeypatch, aeri
):
    rig = preview_rig(tmp_path, monkeypatch, ends_on=None)
    rig.running()
    with pytest.raises(AeriRefuse) as caught:
        aeri.start()
    exc = caught.value
    assert (exc.status, exc.code, exc.extra) == (409, "camera_preview_busy", {})
    assert isinstance(exc, aeri_run.CameraPreviewBusy)
    assert aeri.spawned == [] and aeri.sup.active() is None
    assert rig.mgr._held == 0
    assert aeri_run.http(exc).detail["code"] == "camera_preview_busy"
    # not one of the terminal's own refusals, which its samples list
    assert "camera_preview_busy" not in st.REFUSALS
    rig.procs[0].code = -9
    rig.mgr.tick()
    assert aeri.start()["state"] == "launching"


def test_no_preview_starts_while_terminal_d_is_being_launched(
    tmp_path, monkeypatch, aeri
):
    rig = preview_rig(tmp_path, monkeypatch)
    answers = []
    original = aeri.host.spawn

    def spawn(*args, **kwargs):
        with pytest.raises(preview.Refused) as caught:
            rig.mgr.start()
        answers.append(caught.value.code)
        return original(*args, **kwargs)

    aeri.host.spawn = spawn
    aeri.start()
    assert answers == ["client_starting"]


def test_while_terminal_d_is_active_a_preview_is_refused_by_the_real_rule(
    tmp_path, monkeypatch, aeri
):
    rig = preview_rig(tmp_path, monkeypatch)
    monkeypatch.setattr(run_api, "supervisor", lambda: RunSup())
    monkeypatch.setattr(aeri_run_api, "supervisor", lambda: aeri.sup)
    monkeypatch.setattr(deploy_api, "supervisor", lambda: DeploySup(status()))
    rig.host.blockers = preview.blockers
    aeri.start()
    with pytest.raises(preview.Refused) as caught:
        rig.mgr.start()
    assert caught.value.code == "aeri_active"


def test_the_machine_helpers_are_the_ones_of_their_own_tests():
    assert Machine.__module__ == "test_marvel0_run"
    assert AeriMachine.__module__ == "aeri_machine"
