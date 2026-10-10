# ruff: noqa: F811 - the fixtures imported from test_launch
"""The runner and its command channel (T-CL-08, levi/automatic/runner.py and
control.py): commands reach the run through the run folder and are carried
out once; SIGTERM stops the run in a controlled way; a runner that crashed
is brought back by ``attach`` into FAULT_LOCKED (recovery_ambiguous) with no
motion; commands left behind by a crashed runner are judged by the next one
(a stale ``expected_seq`` is refused). Dry runs on the fakes only; the
``systemd-run`` here is a fake that at most starts the runner as a plain
child process."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from test_launch import (  # noqa: F401 - fixtures
    aeri_home,
    calls,
    fake_systemd,
    no_tracking,
    request,
    write_job,
)

from levi.automatic import control, launch, runner
from levi.automatic.journal import Journal

ROOT = Path(__file__).resolve().parents[2]


def wait_until(check, timeout=30.0, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(step)
    raise AssertionError("timed out")


def state_of(run_dir):
    return Journal.read(run_dir).effective_state


def committed(run_dir):
    return [
        (e.from_state, e.to_state, e.reason, e.authority.command_id)
        for e in Journal.read(run_dir).events
        if e.record == "committed"
    ]


def prepare(job, fake_systemd, **kw):
    """A launched run whose runner the fake systemd-run did not start."""
    found = launch.plan(request(job, **kw))
    handle = launch.launch(
        request(job, backend="systemd", **kw),
        plan_sha256=found.plan_sha256,
        launch_token=found.launch_token,
    )
    return found, Path(handle.run_dir)


class Served:
    """The runner of a prepared run in a thread of this process."""

    def __init__(self, run_dir, digest, **kw):
        self.result = None
        self.thread = threading.Thread(
            target=self._go, args=(run_dir, digest), kwargs=kw, daemon=True
        )
        self.thread.start()

    def _go(self, run_dir, digest, **kw):
        self.result = runner.serve(run_dir, digest, **{"deadline_s": 60, **kw})

    def join(self, timeout=60):
        self.thread.join(timeout)
        assert not self.thread.is_alive()
        return self.result


def human_job(tmp_path, name="r-run", **kw):
    return write_job(tmp_path, name=name, strategy="human_assisted", **kw)


def waiting_run(tmp_path, fake_systemd, name="r-run"):
    job = human_job(tmp_path, name)
    found, run_dir = prepare(
        job, fake_systemd, overrides={"scenes": ["reset_required"]}
    )
    served = Served(run_dir, found.plan_sha256)
    wait_until(lambda: state_of(run_dir) == "WAIT_HUMAN")
    return found, run_dir, served


def send(run_dir, value, timeout=30):
    control.write_command(run_dir, value)
    return wait_until(
        lambda: control.read_result(run_dir, value["command_id"]), timeout
    )


def resume_cmd(run_dir, command_id="resume-1", seq=None):
    seq = len(Journal.read(run_dir).events) if seq is None else seq
    return control.command(
        "resume",
        command_id,
        "op-1",
        expected_seq=seq,
        environment_handled=True,
        health_rechecked=True,
    )


# --- commands through the channel --------------------------------------------------------------------


def test_resume_through_the_channel_completes_the_run(
    tmp_path, fake_systemd, aeri_home
):
    _, run_dir, served = waiting_run(tmp_path, fake_systemd)
    result = send(run_dir, resume_cmd(run_dir))
    assert result["ok"] and result["code"] == "resumed" and not result["repeated"]
    done = served.join()
    assert done.state == "COMPLETED" and done.exit_code == 0
    path = committed(run_dir)
    assert ("WAIT_HUMAN", "PREFLIGHT", "human_resumed", "resume-1") in path
    # Audited in the journal (who, the id, the result) and in control.jsonl.
    notes = [
        e
        for e in Journal.read(run_dir).events
        if e.record == "note" and e.note.code == "operator_command"
    ]
    assert (
        len(notes) == 1 and "resume resume-1 by op-1: resumed" in notes[0].note.detail
    )
    assert notes[0].authority.principal_id == "op-1"
    assert notes[0].authority.command_id is None
    lines = [
        json.loads(x) for x in (aeri_home / "control.jsonl").read_text().splitlines()
    ]
    assert {(x["command_id"], x["code"]) for x in lines if x["phase"] == "result"} == {
        ("resume-1", "resumed")
    }
    record = json.loads((run_dir / "runner.json").read_text())
    assert record["state"] == "exited" and record["final_state"] == "COMPLETED"


def test_a_double_click_resumes_once(tmp_path, fake_systemd):
    _, run_dir, served = waiting_run(tmp_path, fake_systemd)
    value = resume_cmd(run_dir)
    assert control.write_command(run_dir, value) == "queued"
    assert control.write_command(
        run_dir, {**value, "issued_wall_ns": time.time_ns()}
    ) == ("already_queued")
    result = wait_until(lambda: control.read_result(run_dir, "resume-1"))
    assert result["code"] == "resumed"
    served.join()
    resumes = [c for c in committed(run_dir) if c[2] == "human_resumed"]
    assert len(resumes) == 1


def test_a_resume_processed_again_is_repeated_not_redone(tmp_path, fake_systemd):
    """A runner that crashed after the resume but before writing its result
    sees the command again: the orchestrator answers ``repeated``."""
    job = human_job(tmp_path, "r-twice")
    found, run_dir = prepare(
        job, fake_systemd, overrides={"scenes": ["reset_required", "reset_required"]}
    )
    served = Served(run_dir, found.plan_sha256)
    wait_until(lambda: state_of(run_dir) == "WAIT_HUMAN")
    first = send(run_dir, resume_cmd(run_dir))
    assert first["code"] == "resumed"
    wait_until(
        lambda: len([c for c in committed(run_dir) if c[1] == "WAIT_HUMAN"]) == 2
    )
    (run_dir / "control/results/resume-1.json").unlink()
    again = wait_until(lambda: control.read_result(run_dir, "resume-1"))
    assert again["ok"] and again["code"] == "repeated" and again["repeated"]
    assert again["sequence_no"] == first["sequence_no"]
    assert len([c for c in committed(run_dir) if c[3] == "resume-1"]) == 1
    send(run_dir, control.command("stop", "stop-1", "op-1"))
    served.join()


def test_a_stale_expected_seq_is_refused(tmp_path, fake_systemd):
    _, run_dir, served = waiting_run(tmp_path, fake_systemd)
    result = send(run_dir, resume_cmd(run_dir, "resume-old", seq=3))
    assert not result["ok"] and result["code"] == "stale_sequence"
    assert state_of(run_dir) == "WAIT_HUMAN"
    result = send(run_dir, resume_cmd(run_dir, "resume-new"))
    assert result["code"] == "resumed"
    served.join()


def test_missing_confirmations_are_refused(tmp_path, fake_systemd):
    _, run_dir, served = waiting_run(tmp_path, fake_systemd)
    value = {**resume_cmd(run_dir, "resume-half"), "health_rechecked": False}
    result = send(run_dir, value)
    assert (
        result["code"] == "confirmations_missing" and state_of(run_dir) == "WAIT_HUMAN"
    )
    send(run_dir, control.command("stop", "stop-1", "op-1"))
    assert served.join().state == "COMPLETED"


def test_stop_while_waiting_ends_the_run(tmp_path, fake_systemd):
    _, run_dir, served = waiting_run(tmp_path, fake_systemd)
    result = send(run_dir, control.command("stop", "stop-1", "op-1"))
    assert result["ok"] and result["code"] == "completed"
    done = served.join()
    assert done.state == "COMPLETED"
    assert committed(run_dir)[-1][:3] == ("WAIT_HUMAN", "COMPLETED", "operator_stop")
    # A command after the end gets its result too.
    late = control.command("stop", "stop-2", "op-1")
    control.write_command(run_dir, late)
    assert control.read_result(run_dir, "stop-2") is None  # no runner any more


def test_invalid_command_files_get_an_invalid_result(tmp_path, fake_systemd):
    _, run_dir, served = waiting_run(tmp_path, fake_systemd)
    inbox = run_dir / "control" / "inbox"
    (inbox / "bad-1.json").write_text('{"kind": "resume", "kind": "stop"}')
    (inbox / "not a name.json").write_text("{}")
    target = tmp_path / "outside.json"
    target.write_text(json.dumps(control.command("stop", "evil-1", "op-1")))
    (inbox / "evil-1.json").symlink_to(target)
    assert (
        wait_until(lambda: control.read_result(run_dir, "bad-1"))["code"] == "invalid"
    )
    assert (
        wait_until(lambda: control.read_result(run_dir, "evil-1"))["code"] == "invalid"
    )
    assert state_of(run_dir) == "WAIT_HUMAN"  # a linked stop never acts
    send(run_dir, control.command("stop", "stop-1", "op-1"))
    served.join()
    assert sorted(p.name for p in (run_dir / "control/results").iterdir()) == [
        "bad-1.json",
        "evil-1.json",
        "stop-1.json",
    ]


@pytest.mark.parametrize("attested", [False, True])
def test_a_scene_answer_reaches_a_person_check_only(tmp_path, fake_systemd, attested):
    job = human_job(tmp_path, "r-ans")
    if attested:
        job.write_text(
            job.read_text().replace(
                "on_unknown: reset",
                "on_unknown: reset\n  scene_check: operator_attested",
            )
        )
    found, run_dir = prepare(
        job, fake_systemd, overrides={"scenes": ["reset_required"]}
    )
    served = Served(run_dir, found.plan_sha256)
    wait_until(lambda: state_of(run_dir) == "WAIT_HUMAN")
    value = control.command(
        "scene_answer",
        "answer-1",
        "op-1",
        request_id="r-ans.forward.0001:scene9",
        predicates={"object_at_source": True},
    )
    result = send(run_dir, value)
    # The scripted person's question was answered and closed already.
    assert result["code"] == ("unsolicited" if attested else "not_supported")
    assert not result["ok"]
    send(run_dir, control.command("stop", "stop-1", "op-1"))
    served.join()


def test_a_second_runner_of_a_live_run_is_refused(tmp_path, fake_systemd):
    found, run_dir, served = waiting_run(tmp_path, fake_systemd)
    before = (run_dir / "runner.json").read_bytes()
    second = runner.serve(run_dir, found.plan_sha256, attach=True)
    assert second.code == "E_RUNNER_ALIVE" and second.exit_code == 2
    assert (run_dir / "runner.json").read_bytes() == before
    with pytest.raises(launch.LaunchRefused) as caught:
        launch.attach(run_dir, backend="foreground")
    assert caught.value.code == "E_RUNNER_ALIVE"
    send(run_dir, control.command("stop", "stop-1", "op-1"))
    served.join()
    with pytest.raises(launch.LaunchRefused) as caught:
        launch.attach(run_dir, backend="foreground")
    assert caught.value.code == "E_RUN_COMPLETED"


def test_the_runner_is_not_the_products_child(tmp_path, fake_systemd, no_tracking):
    _, run_dir, served = waiting_run(tmp_path, fake_systemd)
    send(run_dir, control.command("stop", "stop-1", "op-1"))
    served.join()


# --- processes: SIGTERM, a crashed runner, attach ------------------------------------------------------


def runner_pid(run_dir):
    def found():
        try:
            record = json.loads((Path(run_dir) / "runner.json").read_text())
        except (OSError, ValueError):
            return None
        return record["pid"] if record.get("state") == "running" else None

    return wait_until(found)


def kill_quietly(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def detached_waiting_run(tmp_path, fake_systemd, monkeypatch, name="r-unit"):
    monkeypatch.setenv("FAKE_SYSTEMD_EXEC", "1")
    job = human_job(tmp_path, name)
    found, run_dir = prepare(
        job, fake_systemd, overrides={"scenes": ["reset_required"]}
    )
    pid = runner_pid(run_dir)
    wait_until(lambda: state_of(run_dir) == "WAIT_HUMAN")
    return found, run_dir, pid


def test_sigterm_stops_the_run_and_the_runner(
    tmp_path, fake_systemd, monkeypatch, aeri_home
):
    _, run_dir, pid = detached_waiting_run(tmp_path, fake_systemd, monkeypatch)
    try:
        assert launch.runner_alive(json.loads((run_dir / "runner.json").read_text()))
        os.kill(pid, signal.SIGTERM)
        record = wait_until(
            lambda: (
                json.loads((run_dir / "runner.json").read_text())
                if json.loads((run_dir / "runner.json").read_text())["state"]
                == "exited"
                else None
            )
        )
    finally:
        kill_quietly(pid)
    assert record["exit_code"] == 0 and record["final_state"] == "COMPLETED"
    assert committed(run_dir)[-1][:3] == ("WAIT_HUMAN", "COMPLETED", "operator_stop")
    results = list((run_dir / "control/results").iterdir())
    assert len(results) == 1
    result = json.loads(results[0].read_text())
    assert result["principal_id"] == "system:sigterm" and result["code"] == "completed"
    notes = [
        e.note.detail
        for e in Journal.read(run_dir).events
        if e.record == "note" and e.note.code == "operator_command"
    ]
    assert any("by system:sigterm: completed" in n for n in notes)
    audit = (aeri_home / "control.jsonl").read_text()
    assert "system:sigterm" in audit


def test_a_crashed_runner_is_attached_into_fault_locked_without_motion(
    tmp_path, fake_systemd, monkeypatch
):
    found, run_dir, pid = detached_waiting_run(tmp_path, fake_systemd, monkeypatch)
    os.kill(pid, signal.SIGKILL)
    wait_until(
        lambda: (
            not launch.runner_alive(json.loads((run_dir / "runner.json").read_text()))
        )
    )
    # A resume written before the crash, never processed: judged later.
    stale = resume_cmd(run_dir, "resume-before-crash")
    control.write_command(run_dir, stale)
    handle = launch.attach(run_dir, backend="foreground", deadline_s=1.0)
    assert handle.final_state == "FAULT_LOCKED"
    record = json.loads((run_dir / "runner.json").read_text())
    assert record["attach"] and record["robot_motions"] == 0
    path = committed(run_dir)
    assert path[-1][1:3] == ("FAULT_LOCKED", "recovery_ambiguous")
    assert (
        control.read_result(run_dir, "resume-before-crash")["code"] == "stale_sequence"
    )
    # An operator's fresh resume leads back through PREFLIGHT.
    served = Served(run_dir, found.plan_sha256, attach=True)
    wait_until(lambda: runner_pid(run_dir))
    result = send(run_dir, resume_cmd(run_dir, "resume-after"))
    assert result["code"] == "resumed"
    # Through PREFLIGHT and a fresh initial-state check (the restored fake
    # scene answers its script again: reset_required, so a person again).
    wait_until(lambda: committed(run_dir)[-1][1] == "WAIT_HUMAN")
    tail = [c[:3] for c in committed(run_dir)][-3:]
    assert tail == [
        ("FAULT_LOCKED", "PREFLIGHT", "human_resumed"),
        ("PREFLIGHT", "VERIFY_INITIAL", "preflight_passed"),
        ("VERIFY_INITIAL", "WAIT_HUMAN", "scene_reset_required"),
    ]
    send(run_dir, control.command("stop", "stop-after", "op-1"))
    assert served.join().state == "COMPLETED"


KILLER = """
import os, signal, sys
from levi.automatic import runner
count = {"n": 0}
def hook(point):
    count["n"] += 1
    if count["n"] == int(sys.argv[3]):
        os.kill(os.getpid(), signal.SIGKILL)
runner.serve(sys.argv[1], sys.argv[2], crash_hook=hook, deadline_s=30)
"""


@pytest.mark.parametrize("at", [1, 4, 9, 16, 27, 41])
def test_a_runner_killed_mid_run_is_attached_into_fault_locked(
    tmp_path, fake_systemd, at
):
    job = write_job(tmp_path, name="r-kill")
    found, run_dir = prepare(job, fake_systemd, overrides={"episodes": 3})
    done = subprocess.run(
        [sys.executable, "-c", KILLER, str(run_dir), found.plan_sha256, str(at)],
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(ROOT), os.environ.get("PYTHONPATH", "")]
            ),
        },
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == -9, done.stderr[-500:]
    before = Journal.read(run_dir)
    handle = launch.attach(run_dir, backend="foreground", deadline_s=0.5)
    record = json.loads((run_dir / "runner.json").read_text())
    assert record["robot_motions"] == 0
    assert handle.final_state == "FAULT_LOCKED"
    after = Journal.read(run_dir)
    assert after.corrupt is None
    last = [e for e in after.events if e.record == "committed"][-1]
    assert (last.to_state, last.reason) == ("FAULT_LOCKED", "recovery_ambiguous")
    assert last.authority.principal_kind == "recovery"
    # Nothing replayed: every line the dead runner wrote is still there.
    assert [e.sequence_no for e in after.events[: len(before.events)]] == [
        e.sequence_no for e in before.events
    ]


def test_sigterm_before_an_episode_stops_at_a_person_not_at_the_end(
    tmp_path, fake_systemd
):
    """SIGTERM while the run would start episodes: the stop is registered
    (system:sigterm) and the run stops at WAIT_HUMAN (operator_stop); the
    run is not completed, so it can be attached and resumed later."""
    job = write_job(tmp_path, name="r-term")
    found, run_dir = prepare(job, fake_systemd)
    served = runner.Runner(run_dir, found.plan_sha256)
    assert served.open() is None
    served.sigterm.set()
    served.pump = control.CommandPump(served)
    served.pump.tick()  # what the pump thread does within 50 ms
    result = served.drive(5)
    assert result.exit_code == 0 and result.state == "WAIT_HUMAN"
    assert committed(run_dir)[-1][1:3] == ("WAIT_HUMAN", "operator_stop")
    assert result.robot_motions == 0
    (stop,) = (run_dir / "control/results").iterdir()
    found_result = json.loads(stop.read_text())
    assert found_result["principal_id"] == "system:sigterm"
    assert found_result["code"] == "stop_requested"
