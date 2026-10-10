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
from pathlib import Path

import pytest
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

from levi.automatic import api, control, launch, runner
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
    def __init__(self, run_dir, digest):
        self.result = None
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
    client.post(
        f"{BASE}/runs/r-wait/stop",
        json={"command_id": "stop-cleanup", "confirm": "stop"},
    )
    served.thread.join(30)


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
