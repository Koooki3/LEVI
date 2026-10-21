"""The hosted AERI terminal D as the rest of the product sees it
(``levi/marvel0/aeri_activity.py`` and the places that call it): ``levi stop``
and the R6 restart (``levi.activity.running_jobs``), the service's start and
shutdown (``levi.service.lifespan``), the start check of the evaluation client
(terminal E) and of the policy-server deploy, and the start gate's mechanical
check that the first of these can see a run at all.

The run is real: ``hostchild.py`` and the fake terminal D (``fake_aeri.py``) under
real ptys, driven by the page's own ``Supervisor`` (``aeri_machine.py``); nothing
of the robot is touched. What is asked is the real ``levi.activity.running_jobs``,
the real ``agent.core.stop``, the real ``Supervisor.stop`` and the real start
checks of E and of the deploy. Each of these is also the target of a mutation of
``mutate_aeri_wiring.py``."""

import ast
import contextlib
import json
import os
import signal
import time
from pathlib import Path

import pytest
from aeri_machine import AeriMachine
from conftest import scaled
from test_marvel0_run import Machine as EvalMachine

from levi import activity, paths
from levi.agent import core
from levi.marvel0 import aeri_activity, aeri_run, aeri_run_api, deploy, record, run
from levi.marvel0 import aeri_state as st
from levi.workspace import Workspace

pytestmark = pytest.mark.skipif(
    not os.path.exists("/dev/ptmx"), reason="needs a pseudo-terminal"
)
FAST = (("SETTLE_S", 0.15), ("ARM_DELAY_S", 0.15), ("KEY_GAP_S", 0.3))
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(name="live")
def live_machine(tmp_path, monkeypatch):
    for key, value in FAST:
        monkeypatch.setattr(st, key, value)
    m = AeriMachine(tmp_path / "a", monkeypatch, live=True)
    m.prepare(rounds=2, steps=200)
    # What `levi stop` and the other processes read: this workspace.
    monkeypatch.setattr(paths, "workspace", lambda: m.workspace.root)
    yield m
    m.close()
    for child in m.children:
        with contextlib.suppress(OSError):
            os.killpg(child.pid, signal.SIGKILL)


@pytest.fixture(name="scripted")
def scripted_machine(tmp_path, monkeypatch):
    for key, value in FAST:
        monkeypatch.setattr(st, key, value)
    m = AeriMachine(tmp_path / "s", monkeypatch)
    monkeypatch.setattr(paths, "workspace", lambda: m.workspace.root)
    yield m
    m.close()


def hosted(rows):
    return [r for r in rows if r["kind"] == aeri_activity.KIND]


def to_forward(m, steps=4000):
    m.prepare(rounds=2, steps=steps)
    m.start()
    m.wait_state("await_phrase")
    m.press("execute")
    m.wait_state("await_inventory", timeout=30)
    m.press("inventory_yes")
    m.wait_state("await_release")
    m.press("release_round")
    return m.wait_state("forward_running")


def notes(m, code) -> list:
    path = m.run_dir / "state_journal.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    return [
        r["note"]["detail"] for r in rows if r.get("note") and r["note"]["code"] == code
    ]


# --- levi stop and the restart see the hosted D -----------------------------------------


def test_the_jobs_list_the_hosted_d_from_the_start_click_to_its_end(live):
    m = live
    assert hosted(activity.running_jobs()) == []
    m.start()
    (row,) = hosted(activity.running_jobs())
    rec = m.record()
    assert row["id"] == rec["run_id"] and row["pid"] == rec["leader"]["pid"]
    assert row["kind"] == "marvel0-aeri" and row["hosted"] is True
    assert "marvel0-aeri" in activity.describe([row])
    m.wait_state("await_phrase")
    m.press("execute")
    m.wait_state("await_inventory", timeout=30)
    assert len(hosted(activity.running_jobs())) == 1
    m.press("quit")
    m.wait_state("ended", timeout=30)
    assert hosted(activity.running_jobs()) == [], "an ended run is not a job"


def test_levi_stop_refuses_while_d_lives_and_names_it(live):
    m = live
    m.start()
    m.wait_state("await_phrase")
    answer = core.stop(force=False)
    assert answer["status"] == "refused"
    (row,) = hosted(answer["running"])
    assert row["id"] == m.record()["run_id"]
    assert "marvel0-aeri" in activity.describe(answer["running"])
    assert "levi stop --force" in answer["hint"] or "--force" in answer["hint"]


def test_asking_for_the_jobs_never_takes_the_run_over_or_signals_it(live):
    """A ``levi stop`` process has no supervisor with a keyboard: the page's
    take-over (one SIGINT to a D that lost its keyboard) is wrong there."""
    m = live
    to_forward(m)
    stranger = aeri_run.Supervisor(
        m.host, site_loader=lambda: m.site, workspace=m.workspace
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(aeri_run_api, "supervisor", lambda: stranger)
        for _ in range(3):
            assert len(hosted(activity.running_jobs())) == 1
        assert core.stop(force=False)["status"] == "refused"
    time.sleep(0.5)
    assert stranger._adopted is False, "nothing adopted the run"
    assert m.record().get("stop_sent_epoch") is None
    assert not notes(m, "operator_interrupt"), "D was not told to stop"
    assert m.look()["d"]["alive"] is True


def test_a_record_that_cannot_be_read_is_listed_not_taken_for_no_run(live):
    m = live
    m.start()
    (m.folder / record.RECORD).write_text("{ not json")
    (row,) = hosted(activity.running_jobs())
    assert row["id"] == aeri_activity.UNREADABLE
    assert "marvel0-aeri" in activity.describe([row])  # (no pid to print)
    # the other session's folder is its own: nothing there, nothing listed
    assert [r for r in activity.running_jobs() if r["kind"] != "marvel0-aeri"] == []


def test_a_failure_of_the_reader_is_listed_not_swallowed(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(aeri_activity, "hosted_job", broken)
    rows = activity.running_jobs()
    assert {r["kind"] for r in rows} == {"marvel0-aeri", "marvel0-client"}
    assert {r["id"] for r in rows} == {"error"}


def test_the_reader_reads_the_folder_and_the_identity_the_launch_writes(live):
    assert aeri_activity.REL == aeri_run.REL
    assert aeri_activity.CLIENT_REL == run.REL
    assert record.RECORD == "record.json"
    me = os.getpid()
    assert aeri_activity._identity(me) == run._identity(me)
    m = live
    m.start()
    assert aeri_activity.record_dir(m.workspace) == m.folder
    assert m.record()["leader"]["identity"] == aeri_activity._identity(
        m.record()["leader"]["pid"]
    )


# --- the hosted evaluation client is a job too -------------------------------------------


@pytest.fixture(name="client")
def evaluation_client(tmp_path, monkeypatch):
    m = EvalMachine(tmp_path / "e", monkeypatch, live=True)
    monkeypatch.setattr(paths, "workspace", lambda: m.workspace.root)
    yield m
    m.close()
    for spawned in m.children:
        with contextlib.suppress(OSError):
            os.killpg(spawned.pid, signal.SIGKILL)


def test_the_jobs_list_the_hosted_client_from_its_start_to_its_end(client):
    m = client
    assert activity.running_jobs() == []
    m.fake(connect_s=0.1, mux_s=0.05)
    m.start(max_steps=400)
    m.wait_state("at_gate")
    (row,) = activity.running_jobs()
    rec = m.record()
    assert (row["kind"], row["id"], row["pid"]) == (
        "marvel0-client",
        rec["run_id"],
        rec["leader"]["pid"],
    )
    answer = core.stop(force=False)
    assert answer["status"] == "refused" and answer["running"] == [row]
    m.press("end_at_gate")
    m.wait_state("done")
    assert activity.running_jobs() == []


def test_asking_for_the_jobs_never_freezes_the_hosted_client(client, monkeypatch):
    """The page's take-over of a client freezes it (SIGSTOP); a ``levi stop`` that
    asks must not."""
    m = client
    m.fake(connect_s=0.1, mux_s=0.05)
    m.start(max_steps=400)
    m.wait_state("at_gate")
    pid = m.record()["leader"]["pid"]
    for _ in range(3):
        assert len(activity.running_jobs()) == 1
    time.sleep(0.3)
    assert run.proc_state(pid) not in ("T", None), "not frozen"
    assert m.sup._keyboard is True


# --- the decision on one record -----------------------------------------------------------


def started(identity, pid=4242, **extra):
    return {
        "phase": "started",
        "run_id": "r1",
        "created_epoch": 1000.0,
        "leader": {"pid": pid, "identity": identity},
        **extra,
    }


GOOD = {"start_ticks": "100", "boot": "b1"}


@pytest.mark.parametrize(
    "rec, who, listed",
    [
        (started(GOOD), GOOD, True),
        (started(GOOD), {"start_ticks": "101", "boot": "b1"}, False),  # a later process
        (started(GOOD), {"start_ticks": "100", "boot": "b2"}, False),  # another boot
        (started(GOOD), None, False),  # gone
        (
            started(None),
            {"start_ticks": "1", "boot": "b"},
            True,
        ),  # not verifiable: busy
        (started(None), None, False),
        (started(GOOD, pid=0), GOOD, False),
        ({"phase": "ended", "leader": {"pid": 4242, "identity": GOOD}}, GOOD, False),
        ({"phase": "intent", "created_epoch": 1100.0}, None, True),  # young
        ({"phase": "intent", "created_epoch": 100.0}, None, False),  # a leftover
        ({"phase": "intent"}, None, False),
        ({"phase": "intent", "created_epoch": 2000.0}, None, False),  # from the future
        ({}, GOOD, False),
    ],
)
def test_one_record_is_a_job_exactly_when_its_launcher_is_still_that_process(
    rec, who, listed
):
    row = aeri_activity.row_of(rec, identity=lambda pid: who, now=1150.0)
    assert (row is not None) is listed
    if row:
        assert row["kind"] == "marvel0-aeri" and row["id"] == rec.get("run_id")


# --- the start gate's check ---------------------------------------------------------------


def test_the_gate_check_passes_when_the_wiring_is_whole(live):
    assert aeri_activity.hosted_visible_to_activity(live.workspace) is True


def test_the_gate_check_fails_when_the_source_is_not_wired_in(live, monkeypatch):
    monkeypatch.setattr(activity, "EXTRA_SOURCES", ())
    assert aeri_activity.hosted_visible_to_activity(live.workspace) is False


def test_the_gate_check_fails_when_the_source_forgets_the_run(live, monkeypatch):
    monkeypatch.setattr(activity, "EXTRA_SOURCES", (lambda workspace=None: [],))
    assert aeri_activity.hosted_visible_to_activity(live.workspace) is False


def test_the_gate_check_fails_when_the_folder_is_not_one_it_would_read(live):
    base = live.workspace.state / "marvel0"
    base.mkdir(parents=True, exist_ok=True)
    (base / "elsewhere").mkdir()
    (base / "aeri").symlink_to(base / "elsewhere")
    assert aeri_activity.hosted_visible_to_activity(live.workspace) is False


def test_a_check_that_cannot_run_does_not_pass(live, monkeypatch):
    m = live
    m.start()
    run_id = m.record()["run_id"]

    def broken(*args, **kwargs):
        raise OSError("no")

    monkeypatch.setattr(activity, "running_jobs", broken)
    assert aeri_activity.hosted_visible_to_activity(m.workspace, run_id=run_id) is False
    assert aeri_activity.hosted_visible_to_activity(m.workspace) is False


def test_the_check_after_the_launch_asks_the_real_jobs_for_this_run(live, monkeypatch):
    m = live
    m.start()
    run_id = m.record()["run_id"]
    assert aeri_activity.hosted_visible_to_activity(m.workspace, run_id=run_id) is True
    assert (
        aeri_activity.hosted_visible_to_activity(m.workspace, run_id="other") is False
    )
    monkeypatch.setattr(activity, "EXTRA_SOURCES", ())
    assert aeri_activity.hosted_visible_to_activity(m.workspace, run_id=run_id) is False


def test_the_check_after_the_launch_is_false_when_the_record_is_not_started(live):
    m = live
    assert aeri_activity.hosted_visible_to_activity(m.workspace, run_id="x") is False


# --- the orderly end at the service's shutdown --------------------------------------------


def test_the_shutdown_stops_a_hosted_d_as_a_person_does_and_waits_for_its_wrap_up(
    live,
):
    m = live
    to_forward(m)
    answer = aeri_activity.shutdown_hosted(
        supervisor=m.sup, wait_s=scaled(40), sleep=lambda s: time.sleep(0.05)
    )
    assert answer == {"state": "ended", "stop": "sent"}
    view = m.look()
    assert view["d"]["exit"] == {"code": 130, "signal": None}
    text = m.typescript.read_text()
    assert "7/7" in text, "D ran its shutdown to the end"
    assert "没有停止" not in text, "and no systemctl of it was broken"
    assert notes(m, "operator_interrupt")
    assert [r["key"] for r in m.history() if r["event"] == "stop"] == ["stop"]
    assert [r for r in m.history() if r["event"] == "end"] == []


def test_a_run_that_was_stopped_already_is_waited_for_not_stopped_again(live):
    m = live
    to_forward(m)
    m.stop()
    answer = aeri_activity.shutdown_hosted(
        supervisor=m.sup, wait_s=scaled(40), sleep=lambda s: time.sleep(0.05)
    )
    assert answer == {"state": "ended", "stop": "stop_spent"}
    assert [r["key"] for r in m.history() if r["event"] == "stop"] == ["stop"]


def test_a_d_that_does_not_wrap_up_is_left_alone_after_the_wait(live):
    """No bare kill: what is left is the hang-up chain's, which needs the pty to
    close, not a signal from here."""
    m = live
    m.prepare(rounds=2, steps=4000, hang_after_stop=True)
    m.start()
    m.wait_state("await_phrase")
    m.press("execute")
    m.wait_state("await_inventory", timeout=30)
    m.press("inventory_yes")
    m.wait_state("await_release")
    m.press("release_round")
    m.wait_state("forward_running")
    answer = aeri_activity.shutdown_hosted(
        supervisor=m.sup, wait_s=1.0, sleep=lambda s: time.sleep(0.05)
    )
    assert answer == {"state": "timeout", "stop": "sent"}
    assert not [s for s in m.signals if s[2] == signal.SIGKILL], "nothing was killed"
    assert m.look()["d"]["alive"] is True


def test_with_no_hosted_d_the_shutdown_does_nothing(live):
    m = live
    fresh = aeri_run.Supervisor(
        m.host, site_loader=lambda: m.site, workspace=m.workspace
    )
    assert aeri_activity.shutdown_hosted(supervisor=fresh, wait_s=0.1) == {
        "state": "none",
        "stop": None,
    }
    assert m.signals == []


def test_a_supervisor_that_cannot_answer_never_raises_at_shutdown(live):
    class Broken:
        def active(self):
            raise OSError("no")

    assert aeri_activity.shutdown_hosted(supervisor=Broken())["state"] == "error"


def test_the_start_takes_over_a_d_that_outlived_the_service_with_one_ctrl_c(
    live, monkeypatch
):
    m = live
    to_forward(m)
    time.sleep(0.3)
    new = aeri_run.Supervisor(m.host, site_loader=lambda: m.site, workspace=m.workspace)
    monkeypatch.setattr(aeri_run_api, "supervisor", lambda: new)
    aeri_activity.adopt_on_start()
    assert new._adopted is True and new._rec["stop_sent_epoch"] is not None
    deadline = time.monotonic() + scaled(40)
    while time.monotonic() < deadline:
        new._cache = None
        new.tick()
        if new.view()["state"]["id"] == "ended":
            break
        time.sleep(0.1)
    assert new.view()["d"]["exit"]["code"] == 130
    new._master = None


def test_a_take_over_that_fails_never_stops_the_service_from_starting(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("no")

    # (the page's own adopt_on_start already swallows a failing supervisor)
    monkeypatch.setattr(aeri_run_api, "adopt_on_start", broken)
    assert aeri_activity.adopt_on_start() is None
    monkeypatch.undo()
    monkeypatch.setattr(aeri_run_api, "supervisor", broken)
    assert aeri_activity.adopt_on_start() is None


def lifespan():
    tree = ast.parse((ROOT / "levi" / "service.py").read_text())
    return next(
        n
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan"
    )


def is_call(node, name) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == name
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "aeri_activity"
    )


def test_the_service_adopts_before_it_serves_and_stops_the_d_first_when_it_ends():
    body = lifespan().body
    (guarded,) = [
        n
        for n in body
        if isinstance(n, ast.Try)
        and any(
            isinstance(x, ast.Expr) and isinstance(x.value, ast.Yield) for x in n.body
        )
    ]
    adopted = [i for i, n in enumerate(body) if is_call(n, "adopt_on_start")]
    assert len(adopted) == 1 and adopted[0] < body.index(guarded)
    assert is_call(guarded.finalbody[0], "shutdown_hosted"), (
        "the orderly end of D comes before anything else is stopped"
    )


# --- the evaluation client and the deploy refuse beside it --------------------------------


def e_supervisor(m):
    """Terminal E's real supervisor on a host whose only change is that its
    registry is empty: the probe of the hosted D is the real default."""
    host = run.Host(
        aeri_runs=list,
        now=time.time,
        monotonic=time.monotonic,
        sleep=time.sleep,
    )
    return run.Supervisor(host, site_loader=lambda: m.site, workspace=m.workspace)


def test_a_client_is_not_started_beside_the_hosted_d_from_the_click_that_starts_it(
    scripted, monkeypatch
):
    m = scripted
    monkeypatch.setattr(aeri_run_api, "supervisor", lambda: m.sup)
    sup = e_supervisor(m)
    assert sup._aeri_row()["level"] == "ok"
    m.start()  # the page's record says "started": no registry knows it yet
    row = sup._aeri_row()
    assert (row["code"], row["level"]) == ("no_aeri", "refuse")
    m.child.exits(0)
    m.look()
    m.look()
    assert sup._aeri_row()["level"] == "ok"


def test_a_probe_of_the_hosted_d_that_fails_does_not_rule_it_out(scripted, monkeypatch):
    m = scripted

    def broken():
        raise OSError("no")

    monkeypatch.setattr(aeri_run_api, "supervisor", broken)
    row = e_supervisor(m)._aeri_row()
    assert (row["code"], row["level"]) == ("no_aeri", "unknown")


def deploy_supervisor(m):
    host = deploy.Host(run=lambda argv, timeout: (0, ""), aeri_runs=list)
    return deploy.Supervisor(
        host, site_loader=lambda: m.site, catalog=None, workspace=m.workspace
    )


def test_a_policy_server_is_not_deployed_under_the_hosted_d(scripted, monkeypatch):
    m = scripted
    monkeypatch.setattr(aeri_run_api, "supervisor", lambda: m.sup)
    sup = deploy_supervisor(m)
    sup._check_aeri()  # (nothing hosted: no refusal)
    m.start()
    with pytest.raises(deploy.Refuse) as caught:
        sup._check_aeri()
    assert caught.value.code == "aeri_run_live"
    assert caught.value.extra["run_id"] == m.record()["run_id"]


def test_a_probe_of_the_hosted_d_that_fails_refuses_the_deploy(scripted, monkeypatch):
    m = scripted

    def broken():
        raise OSError("no")

    monkeypatch.setattr(aeri_run_api, "supervisor", broken)
    with pytest.raises(deploy.Refuse) as caught:
        deploy_supervisor(m)._check_aeri()
    assert caught.value.code == "aeri_registry_unreadable"


def test_the_workspace_default_is_the_one_the_page_writes(live):
    """``levi stop`` has no supervisor and no workspace argument: the default
    ``paths.workspace()`` is the page's own (``Supervisor`` with no workspace)."""
    m = live
    assert aeri_activity.record_dir() is None  # (no run yet: no folder)
    m.start()
    assert (
        aeri_activity.record_dir()
        == m.folder
        == aeri_run.state_dir((), Workspace.at(paths.workspace()), create=False)
    )
