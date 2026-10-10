"""The recorder compatibility layer (T-C-10, levi/automatic/recorder.py):
AERI episodes as rollout folders and session files the live service reads
with its own code (levi.live.criteria, levi.live.sessions; nothing there is
changed), the run manifest, write failures and restarts. Fakes only."""

import errno
import json
import time
from pathlib import Path

import pytest
from aeri_harness import RUN, committed, config, fake, results, rig
from test_aeri_orchestrator import check_invariants, no_network  # noqa: F401

from levi.automatic import state_machine as sm
from levi.automatic.adapters import events as ev
from levi.automatic.orchestrator import Orchestrator
from levi.automatic.recorder import (
    MANIFEST,
    RecorderError,
    RolloutRecorder,
    SessionFiles,
    sealed_episodes,
)
from levi.live import config as live_config
from levi.live import criteria, gpumgr, sessions

GROUP = "aeri_fake"
FOLDERS = {"forward": "stack_plates__r-sm", "reset": "reset_stack_plates__r-sm"}
TEXTS = {"forward": "stack the plates", "reset": "Reset: stack the plates"}


class Disk:
    """``io_hook``: fail the Nth matching operation with ENOSPC."""

    def __init__(self, op=None, name=None, at=0):
        self.op, self.name, self.at, self.seen = op, name, at, 0

    def __call__(self, op, path):
        if (self.op is None or op == self.op) and (
            self.name is None or Path(path).name == self.name
        ):
            self.seen += 1
            if self.seen - 1 == self.at:
                raise OSError(errno.ENOSPC, "No space left on device")


def build_run(tmp_path, *, cfg=None, scene=None, events=None, io_hook=None, **kw):
    clock = kw.pop("clock", None) or fake.FakeClock()
    directory = Path(tmp_path) / ".aeri" / "runs" / RUN
    recorder = RolloutRecorder(
        tmp_path,
        run_id=RUN,
        run_dir=directory,
        group=GROUP,
        texts=TEXTS,
        io_hook=io_hook,
    )
    listener = SessionFiles(
        tmp_path, run_id=RUN, group=GROUP, folders=FOLDERS, texts=TEXTS
    )
    cfg = cfg or config(
        episodes=1, forward_folder=FOLDERS["forward"], reset_folder=FOLDERS["reset"]
    )
    r = rig(
        directory,
        cfg=cfg,
        clock=clock,
        recorder=recorder,
        scene=scene or ev.FakeSceneAssessor([], clock, RUN),
        events=events
        or ev.FakeEventStream(
            {
                "forward": [{"step": 8, "event_type": "object_settled"}],
                "reset": [{"step": 6, "event_type": "object_settled"}],
            },
            clock,
            RUN,
        ),
        create=False,
        **kw,
    )
    r.orch = Orchestrator.create(directory, cfg, listener=listener, **r.parts())
    r.listener = listener
    return r


def demo(tmp_path, role, name="demo_0001"):
    return Path(tmp_path) / GROUP / FOLDERS[role] / name


def accepted(path) -> bool:
    return criteria.check(path, now=time.time() + 1).state == "complete"


def reset_first(clock):
    return ev.FakeSceneAssessor(
        [{"decision": "reset_required"}], clock, RUN, default={"decision": "ready"}
    )


# --- folders the live service accepts --------------------------------------------------------


def test_forward_and_reset_rollouts_are_accepted_by_the_live_criteria(tmp_path):
    clock = fake.FakeClock()
    r = build_run(tmp_path, clock=clock, scene=reset_first(clock))
    assert r.orch.run() == "COMPLETED"
    forward, reset = demo(tmp_path, "forward"), demo(tmp_path, "reset")
    for path in (forward, reset):
        assert (path / ".complete").exists() and accepted(path)
    meta = json.loads((forward / "metadata.json").read_text())
    # The automatic verdict is no operator label and no agent label.
    assert (
        meta["eval"]["outcome"] == "unlabeled" and meta["eval"]["verdict_by"] == "aeri"
    )
    assert "agent_label" not in meta["eval"]
    assert criteria.agent_label(meta) is None
    assert criteria.operator_label(meta)["outcome"] == "unlabeled"
    assert meta["eval"]["aeri"]["episode_id"] == f"{RUN}.forward.0001"
    steps = (forward / "aeri_steps.csv").read_text().splitlines()
    assert len(steps) - 1 == meta["frame_count"] > 0
    manifest = json.loads((r.directory / MANIFEST).read_text())
    by_id = {e["episode_id"]: e for e in manifest["episodes"]}
    assert by_id[f"{RUN}.reset.0001"]["after_forward"] is None
    assert by_id[f"{RUN}.forward.0001"]["after_resets"] == [f"{RUN}.reset.0001"]
    assert {e["state"] for e in manifest["episodes"]} == {"complete"}
    assert manifest["folders"] == FOLDERS
    check_invariants(r)


def test_sealing_twice_returns_the_first_result(tmp_path):
    rec = RolloutRecorder(tmp_path, run_id=RUN, run_dir=tmp_path / "run", group=GROUP)
    rollout = rec.open(task_folder="f", episode_id=f"{RUN}.forward.0001", number=1)
    rec.stage(rollout, {"pose": [0.0]})
    rec.commit(rollout, fake.StepResult("yes", "ok", 0))
    first = rec.seal(rollout, {})
    marker = rollout.path / ".complete"
    stamp = marker.stat().st_mtime_ns
    assert rec.seal(rollout, {}) == first and marker.stat().st_mtime_ns == stamp
    with pytest.raises(RecorderError):
        rec.commit(rollout, fake.StepResult("yes", "ok", 1))


@pytest.mark.parametrize(
    "op, name, at",
    [
        ("append", "aeri_steps.csv", 3),
        ("sync", "aeri_steps.csv", 0),
        ("write", "events.csv", 1),
        ("write", "metadata.json", 1),
        ("marker", ".complete", 0),
    ],
)
def test_a_write_failure_never_leaves_a_complete_marker(tmp_path, op, name, at):
    r = build_run(tmp_path, io_hook=Disk(op, name, at))
    assert r.orch.run() == "FAULT_LOCKED"
    assert committed(r.orch)[-1][1:] == ("FAULT_LOCKED", "recorder_failed")
    assert not demo(tmp_path, "forward").exists()
    gone = demo(tmp_path, "forward", "incomplete_0001")
    assert gone.is_dir() and not (gone / ".complete").exists()
    assert criteria.kind_of(gone.name) == "incomplete"
    (result,) = results(r.orch)
    assert result.rollout.sealed == "incomplete" and result.task_outcome == "unknown"
    manifest = json.loads((r.directory / MANIFEST).read_text())
    assert manifest["episodes"][0]["state"] == "incomplete"
    check_invariants(r)


def test_an_existing_rollout_number_is_never_overwritten(tmp_path):
    other = demo(tmp_path, "forward")
    other.mkdir(parents=True)
    (other / "metadata.json").write_text('{"keep": true}')
    r = build_run(tmp_path)
    assert r.orch.run() == "FAULT_LOCKED"
    assert committed(r.orch)[-1][1:] == ("FAULT_LOCKED", "recorder_failed")
    assert json.loads((other / "metadata.json").read_text()) == {"keep": True}
    assert r.robot.motions == []


# --- restarts -------------------------------------------------------------------------------------


def restart(r, tmp_path):
    r.orch.close()
    r.recorder.close()
    recorder = RolloutRecorder(
        tmp_path, run_id=RUN, run_dir=r.directory, group=GROUP, texts=TEXTS
    )
    new = rig(r.directory, cfg=r.config, clock=r.clock, recorder=recorder, create=False)
    new.orch = Orchestrator.restore(
        r.directory, r.config, listener=r.listener, **new.parts()
    )
    return new


def test_a_crash_after_the_marker_but_before_the_commit_renames_the_rollout(tmp_path):
    r = build_run(tmp_path)
    real = r.recorder.seal

    def seal_then_die(rollout, meta):
        real(rollout, meta)
        raise fake.SimulatedCrash("killed after the marker")

    r.recorder.seal = seal_then_die
    with pytest.raises(fake.SimulatedCrash):
        r.orch.run()
    assert (demo(tmp_path, "forward") / ".complete").exists()
    new = restart(r, tmp_path)
    assert new.orch.state == "FAULT_LOCKED"
    # The journal never committed the seal: the folder must not stay usable.
    assert not demo(tmp_path, "forward").exists()
    gone = demo(tmp_path, "forward", "incomplete_0001")
    assert not (gone / ".complete").exists()
    meta = json.loads((gone / "metadata.json").read_text())
    assert meta["eval"]["abort_reason"] == "orchestrator_crash"
    (result,) = results(new.orch)
    assert result.rollout.sealed == "incomplete"
    assert result.stop_reason == "orchestrator_crash"
    assert new.orch.note_counts["rollouts_recovered"] == 1


def test_a_crash_mid_episode_renames_the_open_rollout(tmp_path):
    r = build_run(tmp_path, robot={"step_faults": {4: "crash_after_send"}})
    with pytest.raises(fake.SimulatedCrash):
        r.orch.run()
    assert demo(tmp_path, "forward").is_dir()
    new = restart(r, tmp_path)
    assert not demo(tmp_path, "forward").exists()
    assert demo(tmp_path, "forward", "incomplete_0001").is_dir()
    assert new.orch.run() == "FAULT_LOCKED" and new.robot.motions == []


def test_a_sealed_and_committed_rollout_survives_a_restart(tmp_path):
    clock = fake.FakeClock()
    r = build_run(
        tmp_path,
        clock=clock,
        cfg=config(
            episodes=2,
            forward_folder=FOLDERS["forward"],
            reset_folder=FOLDERS["reset"],
        ),
        robot={"step_faults": {20: "crash_after_send"}},
    )
    with pytest.raises(fake.SimulatedCrash):
        r.orch.run()
    assert sealed_episodes(r.orch.journal.events) == {f"{RUN}.forward.0001"}
    new = restart(r, tmp_path)
    assert accepted(demo(tmp_path, "forward"))
    assert demo(tmp_path, "forward", "incomplete_0002").is_dir()
    manifest = json.loads((r.directory / MANIFEST).read_text())
    assert [e["state"] for e in manifest["episodes"]] == ["complete", "incomplete"]
    new.orch.close()


# --- sessions ------------------------------------------------------------------------------------


def gate_config():
    c = live_config.Config()
    c.gpu.mode = "timeshare"
    return c


def test_session_files_follow_the_run_and_keep_the_gate_truthful(tmp_path):
    clock = fake.FakeClock()
    r = build_run(tmp_path, clock=clock, scene=reset_first(clock))
    seen = []
    original = r.listener.write

    def spy(state, **info):
        written = original(state, **info)
        sessions._CACHE.clear()
        found = sessions.read_sessions([tmp_path], now=time.time() + 1)
        states = {s.task_folder: s.raw_state for s in found.values()}
        gate = gpumgr.gate(gate_config(), "timeshare", found, True)
        seen.append((state, states, gate.code == "policy_inferring"))
        return written

    r.listener.write = spy
    assert r.orch.run() == "COMPLETED"
    assert r.orch.note_counts["session_write_failed"] == 0
    by_state = {}
    for state, states, inferring in seen:
        by_state.setdefault(state, set()).add(
            (tuple(sorted(states.items())), inferring)
        )
    # The gate is closed for inference exactly while a policy may infer.
    for state, found in by_state.items():
        assert {i for _, i in found} == {state in sm.POLICY_MAY_INFER}, state
    forward, reset = FOLDERS["forward"], FOLDERS["reset"]
    (fa,) = by_state["FORWARD_ACTIVE"]
    assert dict(fa[0]) == {forward: "running", reset: "standby"}
    (ra,) = by_state["RESET_ACTIVE"]
    assert dict(ra[0]) == {forward: "standby", reset: "running"}
    (done,) = by_state["COMPLETED"]
    assert dict(done[0]) == {forward: "finished", reset: "finished"}
    body = json.loads(r.listener.path("reset").read_text())
    assert body["levi"] == {"enabled": False, "reset_wait_s": None}
    assert body["episode_role"] == "reset" and body["aeri"]["run_id"] == RUN


def test_a_failing_session_write_never_stops_the_run(tmp_path):
    r = build_run(tmp_path)

    def broken(state, info):
        raise OSError(errno.EROFS, "read-only")

    r.orch.listener = broken
    assert r.orch.run() == "COMPLETED"
    assert r.orch.note_counts["session_write_failed"] > 0


# --- self-audit regressions ----------------------------------------------------------------------


@pytest.mark.parametrize("op, name", [("mkdir", "demo_0001"), ("write", "events.csv")])
def test_an_episode_that_cannot_open_is_left_incomplete_not_opening(tmp_path, op, name):
    r = build_run(tmp_path, io_hook=Disk(op, name, 0))
    assert r.orch.run() == "FAULT_LOCKED"
    assert committed(r.orch)[-1][1:] == ("FAULT_LOCKED", "recorder_failed")
    manifest = json.loads((r.directory / MANIFEST).read_text())
    (entry,) = manifest["episodes"]
    assert entry["state"] == "incomplete" and entry["abort_reason"] == "open_failed"
    assert not demo(tmp_path, "forward").exists()
    assert r.robot.motions == []


def test_a_taken_number_never_abandons_the_other_folder(tmp_path):
    rec = RolloutRecorder(tmp_path, run_id=RUN, run_dir=tmp_path / "run", group=GROUP)
    first = rec.open(task_folder="f", episode_id=f"{RUN}.forward.0001", number=1)
    with pytest.raises(RecorderError):
        rec.open(task_folder="f", episode_id=f"{RUN}.forward.0001", number=1)
    assert first.path.is_dir() and first.state == "open"


def test_recover_finds_a_folder_an_abort_renamed_before_the_manifest(tmp_path):
    rec = RolloutRecorder(tmp_path, run_id=RUN, run_dir=tmp_path / "run", group=GROUP)
    rollout = rec.open(task_folder="f", episode_id=f"{RUN}.forward.0001", number=1)
    rollout.handle.close()
    rollout.handle = None
    rollout.path.rename(rollout.path.with_name("incomplete_0001"))  # the crash
    again = RolloutRecorder(tmp_path, run_id=RUN, run_dir=tmp_path / "run", group=GROUP)
    assert again.recover(set()) == []
    (entry,) = json.loads((tmp_path / "run" / MANIFEST).read_text())["episodes"]
    assert (entry["state"], entry["demo"]) == ("incomplete", "incomplete_0001")


def test_heartbeats_from_another_thread_never_tear_a_session_file(tmp_path):
    import threading

    files = SessionFiles(tmp_path, run_id=RUN, group=GROUP, folders=FOLDERS)
    files.write("FORWARD_ACTIVE", control_epoch=1)
    stop = threading.Event()
    seen = []

    def beat():
        while not stop.is_set():
            files.heartbeat()

    def read():
        while not stop.is_set():
            for role in ("forward", "reset"):
                seen.append(json.loads(files.path(role).read_text())["state"])

    threads = [threading.Thread(target=beat), threading.Thread(target=read)]
    for thread in threads:
        thread.start()
    for n in range(200):
        files.write("FORWARD_ACTIVE" if n % 2 else "SCENE_ASSESS", control_epoch=n)
    stop.set()
    for thread in threads:
        thread.join()
    assert seen and set(seen) <= {"running", "standby", "waiting_reset"}
    assert not list((tmp_path / ".eval_sessions").glob(".*.tmp"))
