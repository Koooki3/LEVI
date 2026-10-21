# ruff: noqa: F401, F811, RUF059
"""Starting, stopping and resuming runs through the HTTP interface
(T-API-1). The launch goes through a fake ``systemd-run`` (it only records
its arguments: no unit is ever started); the runner is then served in a
thread of this process, as ``test_runner`` does."""

import json
import os
import stat
import sys
import threading
import time
import warnings
from pathlib import Path

import pytest
from conftest import scaled
from test_api_common import (
    REQ,
    client,
    home,
    job_id_of,
    roots,
    wait_for,
    write_job,
)
from test_launch import FAKE_SYSTEMD

from levi.automatic import api, control, launch, recorder, runner, scene_assessment
from levi.automatic.journal import Journal

BASE = "/api/levi/automatic"


@pytest.fixture
def fake_systemd(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    program = bin_dir / "systemd-run"
    program.write_text(FAKE_SYSTEMD.format(python=sys.executable))
    program.chmod(0o755)
    log = tmp_path / "systemd-run.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SYSTEMD_LOG", str(log))
    monkeypatch.setattr(api, "LAUNCH_BACKEND", "systemd")
    return log


def calls(log: Path) -> list:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def plan_of(client, job_id, mode="dry_run", **extra):
    return client.post(
        f"{BASE}/plan", json={"job_id": job_id, "execution_mode": mode, **extra}
    ).json()


def launch_body(plan, job_id, request_id="req-launch-1", **over):
    return {
        "job_id": job_id,
        "plan_sha256": plan["plan_sha256"],
        "launch_token": plan["launch_token"],
        "confirm": "launch",
        "request_id": request_id,
        **over,
    }


def post_run(client, plan, job_id, **over):
    return client.post(f"{BASE}/runs", json=launch_body(plan, job_id, **over))


# --- starting ----------------------------------------------------------------------------------


def test_a_dry_run_launches_through_systemd_and_is_idempotent(
    client, roots, tmp_path, fake_systemd, home
):
    write_job(roots, tmp_path, name="r-go")
    job_id = job_id_of(client, "r-go")
    plan = plan_of(client, job_id)
    first = post_run(client, plan, job_id)
    assert first.status_code == 202 and first.json() == {"run_id": "r-go"}
    argv = calls(fake_systemd)
    assert len(argv) == 1 and "--user" in argv[0]
    assert any(a.endswith("levi-aeri-r-go") for a in argv[0] if a.startswith("--unit"))
    # The same request again: the same answer, no second unit.
    again = post_run(client, plan, job_id)
    assert again.status_code == 202 and again.json() == first.json()
    assert len(calls(fake_systemd)) == 1
    # Another request for the same run: it exists already.
    clash = post_run(client, plan, job_id, request_id="req-launch-2")
    assert clash.status_code == 409
    assert clash.json()["detail"]["code"] == "run_exists"
    # After a restart (memory gone) the same request still finds its run.
    api._MEMO.items.clear()
    assert post_run(client, plan, job_id).json() == {"run_id": "r-go"}
    snap = client.get(f"{BASE}/runs/r-go").json()
    assert snap["runner"]["alive"] is False
    assert [r["run_id"] for r in client.get(f"{BASE}/runs").json()["runs"]] == ["r-go"]


def test_launch_needs_the_person_and_the_typed_confirmation(
    client, roots, tmp_path, fake_systemd
):
    write_job(roots, tmp_path, name="r-conf")
    job_id = job_id_of(client, "r-conf")
    plan = plan_of(client, job_id)
    bearer = client.post(
        f"{BASE}/runs",
        json=launch_body(plan, job_id),
        headers={"authorization": "Bearer abc"},
    )
    assert bearer.status_code == 403
    assert bearer.json()["detail"]["code"] == "person_only"
    bad = post_run(client, plan, job_id, confirm="yes")
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "confirm_required"
    client.headers.pop("x-levi-ui-token")
    assert post_run(client, plan, job_id).status_code == 401
    assert calls(fake_systemd) == []


def test_a_real_mode_is_501_even_with_a_valid_plan(
    client, roots, tmp_path, fake_systemd
):
    write_job(roots, tmp_path, name="r-real")
    job_id = job_id_of(client, "r-real")
    for mode in ("shadow", "assisted", "autonomous"):
        plan = plan_of(client, job_id, mode)
        assert plan["launchable"] is False
        response = post_run(client, plan, job_id, request_id=f"req-{mode}")
        assert response.status_code == 501, mode
        assert response.json()["detail"]["code"] == "no_robot_adapter"
    # After a restart the server no longer remembers the plan's mode: the
    # job's own mode is tried, and it is still refused.
    api._PLANS.clear()
    api._MEMO.items.clear()
    plan = plan_of(client, job_id, "shadow")
    api._PLANS.clear()
    again = post_run(client, plan, job_id, request_id="req-after-restart")
    assert again.status_code == 501
    assert calls(fake_systemd) == []


def test_a_changed_job_or_a_bad_token_is_412(client, roots, tmp_path, fake_systemd):
    path = write_job(roots, tmp_path, name="r-tok")
    job_id = job_id_of(client, "r-tok")
    plan = plan_of(client, job_id)
    forged = post_run(client, {**plan, "launch_token": "v1.1.deadbeef"}, job_id)
    assert forged.status_code == 412
    assert forged.json()["detail"]["code"] == "plan_changed"
    malformed = post_run(
        client, {**plan, "launch_token": "nonsense"}, job_id, request_id="req-launch-5"
    )
    assert malformed.status_code == 412
    assert malformed.json()["detail"]["code"] == "token_invalid"
    other = post_run(
        client, {**plan, "plan_sha256": "0" * 64}, job_id, request_id="req-launch-3"
    )
    assert other.status_code == 412
    assert other.json()["detail"]["code"] == "plan_changed"
    # The file is touched after the plan was read.
    path.write_text(path.read_text() + "\n# touched\n")
    changed = post_run(client, plan, job_id, request_id="req-launch-4")
    assert changed.status_code == 412
    assert changed.json()["detail"]["code"] == "plan_changed"
    assert calls(fake_systemd) == []


def test_an_expired_token_is_412(client, roots, tmp_path, fake_systemd, monkeypatch):
    write_job(roots, tmp_path, name="r-exp")
    job_id = job_id_of(client, "r-exp")
    monkeypatch.setattr(launch, "TOKEN_TTL_NS", 1)
    plan = plan_of(client, job_id)
    time.sleep(0.01)
    response = post_run(client, plan, job_id)
    assert response.status_code == 412
    assert response.json()["detail"]["code"] == "token_expired"


def test_a_missing_systemd_is_503(client, roots, tmp_path, monkeypatch, home):
    write_job(roots, tmp_path, name="r-nosd")
    job_id = job_id_of(client, "r-nosd")
    monkeypatch.setattr(api, "LAUNCH_BACKEND", "systemd")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    plan = plan_of(client, job_id)
    response = post_run(client, plan, job_id)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "systemd_unavailable"


# --- a run that waits for a person -----------------------------------------------------------


class Served:
    """The runner of a launched run in a thread of this process, with its
    command pump (the ``aeri-control-<run>`` thread ``runner.serve`` starts)."""

    def __init__(self, run_dir, digest):
        self.result = None
        self.run_dir = Path(os.path.abspath(run_dir))
        self.forced = False
        self.thread = threading.Thread(
            target=self._go, args=(run_dir, digest), daemon=True
        )
        self.thread.start()

    def _go(self, run_dir, digest):
        self.result = runner.serve(run_dir, digest, deadline_s=60)

    def join(self, timeout=60):
        self.thread.join(timeout)
        assert not self.thread.is_alive()
        return self.result

    def pumps(self):
        """The live command pumps of this run's runner: found by their type
        and run folder (the runner object is local to ``runner.serve``)."""
        return [
            t
            for t in threading.enumerate()
            if isinstance(t, control.CommandPump) and t.run_dir == self.run_dir
        ]

    def close(self, stop=None, timeout=None, grace=None, strict=False):
        """End the runner and wait for its threads; returns those still
        running (each wait at most ``timeout``, default scaled(30)).

        ``stop()`` asks for the stop as a person would (the HTTP stop) and
        returns what to say if it did not work. A runner still serving
        ``grace`` (default ``timeout``) after it is ended as by SIGTERM, with
        a warning (``forced``): a run that waits for a person then exits
        without a stop (``exited_without_stop``). ``strict``: once the
        threads are ended, a forced end fails the test (``pytest.fail``) with
        the stop's answer and the time waited: the stop is the path that
        stops the arm on the real robot, and must not just warn.
        """
        began = time.monotonic()
        timeout = scaled(30) if timeout is None else timeout
        said = stop() if stop is not None else "no stop was sent"
        self.thread.join(timeout if grace is None else grace)
        if self.thread.is_alive():
            self.forced = True
            warnings.warn(
                f"the served run {self.run_dir.name} did not end after the stop "
                f"({said}); ending its runner as a SIGTERM would"
            )
            for pump in self.pumps():
                pump.runner.sigterm.set()
                pump.runner.wake.set()
            self.thread.join(timeout)
        for pump in self.pumps():
            pump.join(timeout)
        left = [t for t in (self.thread, *self.pumps()) if t.is_alive()]
        if left:
            warnings.warn(f"the served run's threads outlived teardown: {left}")
        if strict and self.forced:
            pytest.fail(
                f"the served run {self.run_dir.name} did not end after the stop "
                f"({said}); ended as by SIGTERM after waiting "
                f"{time.monotonic() - began:.1f} s in all; threads left: {left}",
                pytrace=False,
            )
        return left


def stop_by_page(client, run_id, command_id):
    """``Served.close``'s ``stop``: the page's stop of ``run_id``."""

    def stop():
        client.headers.update(REQ)
        response = client.post(
            f"{BASE}/runs/{run_id}/stop",
            json={"command_id": command_id, "confirm": "stop"},
        )
        return f"HTTP {response.status_code}: {response.text[:300]}"

    return stop


@pytest.fixture
def waiting(client, roots, tmp_path, fake_systemd):
    """A human-assisted dry run that waits at WAIT_HUMAN, served in a thread."""
    write_job(roots, tmp_path, name="r-wait", strategy="human_assisted")
    job_id = job_id_of(client, "r-wait")
    plan = plan_of(client, job_id, overrides={"scenes": ["reset_required"]})
    assert post_run(client, plan, job_id).status_code == 202
    entry = launch.read_record(launch.index_path("r-wait"))
    served = Served(Path(entry["run_dir"]), plan["plan_sha256"])
    snap = wait_for(
        lambda: (
            (s := client.get(f"{BASE}/runs/r-wait").json())["state"] == "WAIT_HUMAN"
            and s["runner"]["alive"]
            and s
        )
    )
    yield client, "r-wait", snap, served, Path(entry["run_dir"])
    # Whatever a test did, end the run so the thread finishes.
    left = served.close(
        stop_by_page(client, "r-wait", "stop-cleanup"), timeout=scaled(60)
    )
    assert not served.forced, "the served run did not end after the stop"
    assert not left


def _served_waiting(client, roots, tmp_path, name):
    """A human-assisted dry run served in a thread, waiting at WAIT_HUMAN."""
    write_job(roots, tmp_path, name=name, strategy="human_assisted")
    job_id = job_id_of(client, name)
    plan = plan_of(client, job_id, overrides={"scenes": ["reset_required"]})
    assert post_run(client, plan, job_id).status_code == 202
    entry = launch.read_record(launch.index_path(name))
    served = Served(Path(entry["run_dir"]), plan["plan_sha256"])
    wait_for(lambda: client.get(f"{BASE}/runs/{name}").json()["state"] == "WAIT_HUMAN")
    return served


def test_teardown_ends_a_served_runner_no_stop_reached(
    client, roots, tmp_path, fake_systemd
):
    """CI: a fixture's stop did not end the run, and the runner thread and
    its command pump (``aeri-control-<run>``) outlived the test. ``close``
    ends both and waits for them, well within the runner's 60 s deadline."""
    served = _served_waiting(client, roots, tmp_path, "r-close")
    try:
        pumps = served.pumps()
        assert len(pumps) == 1 and pumps[0].name == "aeri-control-r-close"
        began = time.monotonic()
        with pytest.warns(UserWarning, match="did not end after the stop"):
            left = served.close(timeout=scaled(10), grace=0.2)
        took = time.monotonic() - began
        assert served.forced and left == []
        assert not served.thread.is_alive() and not pumps[0].is_alive()
        assert took < scaled(12)  # without the SIGTERM: the 60 s deadline
        # It held: the runner exited and left the run waiting for a person.
        assert served.result is not None and served.result.state == "WAIT_HUMAN"
    finally:
        served.close(stop_by_page(client, "r-close", "stop-end"))


def test_a_strict_teardown_fails_when_the_stop_did_not_end_the_run(
    client, roots, tmp_path, fake_systemd
):
    """The scene fixture's path (``strict``): a stop that did not take effect
    is a test failure naming the stop's answer and the time waited, raised
    only after the runner thread and its pump have ended."""
    served = _served_waiting(client, roots, tmp_path, "r-strict")
    try:
        pumps = served.pumps()
        assert len(pumps) == 1

        def ignored_stop():  # a stop that reaches no runner
            return "HTTP 409: the stop was not carried out"

        with (
            pytest.raises(pytest.fail.Exception) as failed,
            warnings.catch_warnings(),
        ):
            warnings.simplefilter("ignore")
            served.close(ignored_stop, timeout=scaled(10), grace=0.2, strict=True)
        message = str(failed.value)
        assert "HTTP 409: the stop was not carried out" in message
        assert " s in all" in message and "threads left: []" in message
        assert served.forced
        assert not served.thread.is_alive() and not pumps[0].is_alive()
    finally:
        served.close(stop_by_page(client, "r-strict", "stop-end"))


def test_the_waiting_snapshot_carries_the_card_and_a_challenge(waiting):
    client, run_id, snap, _, _ = waiting
    card = snap["pending_card"]
    assert card["reason"] == "scene_reset_required"
    assert card["resume_seq"] == snap["seq"] and card["nth_wait"] >= 1
    assert set(card) == {
        "reason", "resume_seq", "waited_ms", "nth_wait", "last_episode",
        "contract", "assessment", "operator_label",
    }  # fmt: skip
    assert card["contract"]["predicates"]
    assert card["operator_label"]["automatic_verdict"] is None
    assert card["operator_label"]["hidden_until_labelled"] is True
    assert snap["counters"]["planned_interventions"] == 1
    assert snap["runner"]["alive"] is True and snap["runner"]["pid"] == os.getpid()


def test_resume_checks_confirmations_sequence_and_challenge(waiting):
    client, run_id, snap, served, run_dir = waiting
    body = {
        "command_id": "resume-1",
        "expected_seq": snap["seq"],
        "environment_handled": True,
        "health_rechecked": True,
        "challenge": snap["challenge"],
    }
    url = f"{BASE}/runs/{run_id}/resume"
    half = client.post(url, json={**body, "health_rechecked": False})
    assert half.status_code == 409
    assert half.json()["detail"]["code"] == "confirmations_missing"
    stale = client.post(url, json={**body, "expected_seq": snap["seq"] - 1})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_sequence"
    forged = client.post(url, json={**body, "challenge": "x" * 20})
    assert forged.status_code == 409
    assert forged.json()["detail"]["code"] == "stale_sequence"
    assert Journal.read(run_dir).effective_state == "WAIT_HUMAN"
    ok = client.post(url, json=body)
    assert ok.status_code == 200, ok.text
    assert ok.json()["result"] == "applied" and ok.json()["code"] == "resumed"
    # The same command again: the first answer; the challenge was used up.
    assert client.post(url, json=body).json() == ok.json()
    other = client.post(url, json={**body, "command_id": "resume-2"})
    assert other.status_code == 409
    # The scripted scene asks a person once; the run goes on to its end.
    assert served.join().state == "COMPLETED"


def test_resume_of_a_run_that_does_not_wait_is_not_waiting(waiting):
    client, run_id, snap, served, run_dir = waiting
    control_stop = client.post(
        f"{BASE}/runs/{run_id}/stop", json={"command_id": "stop-a", "confirm": "stop"}
    )
    assert (
        control_stop.status_code == 200 and control_stop.json()["result"] == "applied"
    )
    served.join()
    late = client.post(
        f"{BASE}/runs/{run_id}/resume",
        json={
            "command_id": "resume-late",
            "expected_seq": 0,
            "environment_handled": True,
            "health_rechecked": True,
            "challenge": "c" * 20,
        },
    )
    assert late.status_code == 409
    assert late.json()["detail"]["code"] == "not_running"
    # A finished run cannot be attached either, and no unit is started.
    attach = client.post(
        f"{BASE}/runs/{run_id}/attach",
        json={"request_id": "req-att-9", "confirm": "attach"},
    )
    assert attach.status_code == 409
    assert attach.json()["detail"]["code"] == "run_completed"


def test_stop_needs_confirmation_and_is_idempotent(waiting):
    client, run_id, snap, served, run_dir = waiting
    url = f"{BASE}/runs/{run_id}/stop"
    wrong = client.post(url, json={"command_id": "stop-1", "confirm": "yes"})
    assert wrong.status_code == 422
    assert wrong.json()["detail"]["code"] == "confirm_required"
    first = client.post(url, json={"command_id": "stop-1", "confirm": "stop"})
    assert first.status_code == 200 and first.json()["result"] == "applied"
    again = client.post(url, json={"command_id": "stop-1", "confirm": "stop"})
    assert again.json() == first.json()
    served.join()
    ended = client.get(f"{BASE}/runs/{run_id}").json()
    assert ended["state"] == "COMPLETED" and ended["runner"]["alive"] is False
    # A stop after the end: no runner.
    late = client.post(url, json={"command_id": "stop-2", "confirm": "stop"})
    assert late.status_code == 409 and late.json()["detail"]["code"] == "not_running"


def test_one_operation_at_a_time_per_run_is_423(waiting):
    client, run_id, *_ = waiting
    lock = api._OPERATIONS.setdefault(f"run:{run_id}", threading.Lock())
    assert lock.acquire(blocking=False)
    try:
        busy = client.post(
            f"{BASE}/runs/{run_id}/stop",
            json={"command_id": "stop-b", "confirm": "stop"},
        )
        assert busy.status_code == 423 and busy.json()["detail"]["code"] == "busy"
    finally:
        lock.release()


def test_commands_are_audited_and_use_the_ui_principal(waiting, home):
    client, run_id, snap, served, run_dir = waiting
    client.post(
        f"{BASE}/runs/{run_id}/stop", json={"command_id": "stop-aud", "confirm": "stop"}
    )
    served.join()
    lines = [
        json.loads(x) for x in (home / launch.CONTROL_LOG).read_text().splitlines()
    ]
    assert any(
        l.get("phase") == "api_command" and l["command_id"] == "stop-aud" for l in lines
    )
    result = control.read_result(run_dir, "stop-aud")
    assert result["principal_id"] == "ui"


def test_arm_and_disarm_are_501(waiting):
    client, run_id, *_ = waiting
    for verb in ("arm", "disarm"):
        response = client.post(f"{BASE}/runs/{run_id}/{verb}")
        assert response.status_code == 501
        assert response.json()["detail"]["code"] == "no_robot_adapter"


def test_attach_is_refused_while_a_runner_lives(waiting):
    client, run_id, *_ = waiting
    url = f"{BASE}/runs/{run_id}/attach"
    wrong = client.post(url, json={"request_id": "req-att-1", "confirm": "no"})
    assert wrong.status_code == 422
    alive = client.post(url, json={"request_id": "req-att-1", "confirm": "attach"})
    assert alive.status_code == 409
    assert alive.json()["detail"]["code"] == "runner_alive"


def test_pending_card_presentation_preserves_frozen_contract(waiting):
    client, run_id, _, _, run_dir = waiting
    contract_path = run_dir / recorder.EVIDENCE / recorder.INITIAL_STATE
    frozen = contract_path.read_bytes()
    described = json.loads(frozen)
    assert described["format"] == scene_assessment.FORMAT_STATUS
    assert described["view_rule"] == scene_assessment.VIEW_RULE
    digest = Journal.read(run_dir).events[0].header.plan_sha256

    response = client.get(f"{BASE}/runs/{run_id}")
    assert response.status_code == 200, response.text
    shown = response.json()["pending_card"]["contract"]
    assert shown == {
        "id_version": f"{described['id']}@{described['version']}",
        "status": described["status"],
        "predicates": described["required"] + described["optional"],
    }
    for field in ("format", "view_rule"):
        assert described[field] not in response.text
    assert contract_path.read_bytes() == frozen
    assert Journal.read(run_dir).events[0].header.plan_sha256 == digest
