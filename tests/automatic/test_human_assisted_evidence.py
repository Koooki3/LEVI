"""T-CL-04: every scene assessment is kept in ``<run_dir>/evidence/`` (the
refused ones too) with the frames it was decided on, bounded and written
whole; ``pending_card(run_dir)`` reads what a person needs when the run
waits for them, without writing anything."""

import dataclasses
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from aeri_harness import RUN, build, committed, config
from aeri_world import FOLDERS

from levi.automatic import recorder as rec
from levi.automatic import scene_assessment as sa
from levi.automatic.adapters import events as ev
from levi.automatic.adapters import human
from levi.automatic.orchestrator import Orchestrator

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = sa.InitialStateContract(
    contract_id="stack-plates-initial",
    contract_version="1",
    required=("object_at_source", "gripper_open"),
    min_evidence_refs=1,
)
GROUP = "aeri"


def run(tmp_path, script, *, contract=CONTRACT, episodes=2, scene=None, **store):
    cfg = config(
        reset_strategy="human_assisted",
        scene_check="operator_attested" if scene is None else "provider",
        initial_state=contract,
        episodes=episodes,
        forward_folder=FOLDERS["forward"],
        reset_folder=FOLDERS["reset"],
    )
    run_dir = tmp_path / ".aeri" / "runs" / RUN
    recorder = rec.RolloutRecorder(tmp_path, run_id=RUN, run_dir=run_dir, group=GROUP)
    rig = build(tmp_path, cfg=cfg, recorder=recorder, create=False)
    shots = []

    def capture():
        shots.append(len(shots) + 1)
        return {"side": f"frame side {len(shots)}".encode()}

    if scene is None:
        rig.scene = human.HumanSceneProvider(
            contract,
            capture,
            human.ScriptedTransport(script),
            rig.clock,
            RUN,
            wait=rig.clock.advance,
        )
    else:
        rig.scene = scene(rig.clock)
    evidence = rec.EvidenceStore(run_dir, **store) if store else True
    rig.orch = Orchestrator.create(rig.directory, cfg, evidence=evidence, **rig.parts())
    rig.run_dir = run_dir
    return rig


def records(rig):
    return rec.evidence_records(rig.run_dir)


def resume(rig, command="op-1"):
    found = rig.orch.resume(
        command,
        expected_seq=rig.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    )
    assert found.ok
    return found


def test_every_assessment_is_kept_with_its_frames(tmp_path):
    rig = run(tmp_path, ["ready", "reset_required", "ready"])
    assert rig.orch.run() == "WAIT_HUMAN"
    found = records(rig)
    assert [r["decision"] for r in found] == ["ready", "reset_required"]
    refused = found[-1]
    assert refused["provider"] == "human" and refused["reason"] == "provider"
    assert refused["failed_predicates"] == ["object_at_source"]
    assert refused["unknown_predicates"] == []
    assert refused["contract"] == {"id": "stack-plates-initial", "version": "1"}
    assert refused["target"] == "post_forward"
    (frame,) = refused["frames"]
    assert frame["view"] == "side" and frame["skipped"] is None
    data = (rig.run_dir / "evidence" / frame["file"]).read_bytes()
    assert data == b"frame side 2"
    assert frame["sha256"] == __import__("hashlib").sha256(data).hexdigest()
    path = rig.run_dir / "evidence" / f"{refused['assessment_id']}.json"
    assert json.loads(path.read_text())["schema"] == "levi.aeri.evidence.v1"
    # The journal still decides: a record per assessment, nothing more.
    assert committed(rig.orch)[-1][2] == "scene_reset_required"


def test_unavailable_and_refused_assessments_are_kept_too(tmp_path):
    rig = run(tmp_path, [None])  # nobody answers: a timeout
    assert rig.orch.run() == "WAIT_HUMAN"
    (timeout,) = records(rig)
    assert (timeout["decision"], timeout["reason"]) == ("unavailable", "timeout")
    assert "assessment_id" not in timeout
    # The frames shown to nobody are kept as well.
    assert timeout["frames"] and timeout["frames"][0]["file"]


@pytest.mark.parametrize(
    ("spec", "reason"),
    [
        ({"decision": "ready", "minor": 1}, "contract_violation:E_SCHEMA_TOO_NEW"),
        (
            {"decision": "ready", "control_key": True},
            "contract_violation:E_CONTROL_FIELD",
        ),
        ({"decision": "ready", "expired": True}, "stale:E_EXPIRED"),
        ({"unavailable": "busy"}, "unavailable:busy"),
        ({"decision": "ready", "evidence": 0}, "insufficient_evidence"),
    ],
)
def test_a_machine_providers_refused_assessment_is_kept(tmp_path, spec, reason):
    def machine(clock):
        return ev.FakeSceneAssessor(
            [spec],
            clock,
            RUN,
            contract=CONTRACT.key,
            predicates=CONTRACT.required,
        )

    rig = run(tmp_path, [], scene=machine)
    rig.orch.run(max_decisions=3)
    first = records(rig)[0]
    assert first["decision"] != "ready" and first["reason"] == reason


def test_frames_are_bounded_and_a_spent_budget_is_said(tmp_path):
    store = rec.EvidenceStore(tmp_path, max_frame_bytes=10, max_total_bytes=4000)
    kept = store.write({"request_id": "q-1", "journal_seq": 1}, {"side:a": b"x" * 11})
    assert kept["frames"][0]["skipped"] == "frame_too_large"
    assert not (tmp_path / "evidence" / "frames").exists()
    small = rec.EvidenceStore(
        tmp_path / "b", max_frame_bytes=5000, max_total_bytes=4000
    )
    first = small.write(
        {"request_id": "q-1", "journal_seq": 1}, {"side:a": b"y" * 3000}
    )
    assert first["frames"][0]["file"]
    second = small.write(
        {"request_id": "q-2", "journal_seq": 2}, {"side:b": b"z" * 3000}
    )
    assert second["frames"][0]["skipped"] == "evidence_budget_exhausted"
    # The records stop at the total; the store never goes over it.
    while small.write({"request_id": "q-3", "journal_seq": 3, "pad": "p" * 50}):
        pass
    assert small.used <= 4000
    total = sum(p.stat().st_size for p in (tmp_path / "b").rglob("*") if p.is_file())
    assert total == small.used <= 4000


def test_the_same_frame_is_kept_once_and_a_record_never_overwritten(tmp_path):
    store = rec.EvidenceStore(tmp_path)
    one = store.write({"request_id": "q-1", "journal_seq": 1}, {"side:a": b"same"})
    two = store.write({"request_id": "q-1", "journal_seq": 2}, {"side:a": b"same"})
    assert one["frames"][0]["file"] == two["frames"][0]["file"]
    assert len(list((tmp_path / "evidence" / "frames").iterdir())) == 1
    names = sorted(p.name for p in (tmp_path / "evidence").glob("*.json"))
    assert names == ["q-1.2.json", "q-1.json"]
    # The budget of a reopened folder counts what is there.
    again = rec.EvidenceStore(tmp_path)
    assert again.used == store.used


def test_a_failed_write_leaves_no_file_half_written(tmp_path):
    def hook(op, path):
        if op == "rename" and path.suffix == ".json":
            raise OSError("disk full")

    store = rec.EvidenceStore(tmp_path, io_hook=hook)
    with pytest.raises(OSError):
        store.write({"request_id": "q-1", "journal_seq": 1}, {"side:a": b"f"})
    folder = tmp_path / "evidence"
    assert not list(folder.glob("*.json")) and not list(folder.glob(".*"))
    assert rec.evidence_records(tmp_path) == []


@pytest.mark.parametrize("victim", ["frame", "record"])
def test_killed_while_writing_evidence(tmp_path, victim):
    """SIGKILL between the temporary file and its rename: no whole file
    appears, the next store removes the temporary one, the card ignores it."""
    code = f"""
import os, signal, sys
from levi.automatic import recorder as rec
def hook(op, path):
    if op == "rename" and (path.suffix == ".json") == ({victim!r} == "record"):
        os.kill(os.getpid(), signal.SIGKILL)
rec.EvidenceStore(sys.argv[1], io_hook=hook).write(
    {{"request_id": "q-1", "journal_seq": 1}}, {{"side:a": b"frame"}})
"""
    proc = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == -signal.SIGKILL, proc.stderr[-2000:]
    folder = tmp_path / "evidence"
    leftovers = [p.name for p in folder.rglob(".*.tmp")]
    assert leftovers
    assert rec.evidence_records(tmp_path) == []
    rec.EvidenceStore(tmp_path)  # the next writer opens the folder
    assert not list(folder.rglob(".*.tmp"))
    assert rec.evidence_records(tmp_path) == []


def test_without_a_store_nothing_is_written(tmp_path):
    cfg = config(initial_state=CONTRACT)
    rig = build(tmp_path, cfg=cfg, create=False)
    rig.orch = Orchestrator.create(rig.directory, cfg, evidence=None, **rig.parts())
    rig.orch.run()
    assert not (rig.directory / "evidence").exists()


# --- the waiting card ------------------------------------------------------------------------------


def snapshot(folder):
    return {
        str(p): (p.stat().st_mtime_ns, p.stat().st_size)
        for p in sorted(Path(folder).rglob("*"))
    }


def test_the_card_says_what_a_person_needs(tmp_path):
    rig = run(tmp_path, ["ready", "reset_required"])
    assert rig.orch.run() == "WAIT_HUMAN"
    wait = rig.orch.journal.events[-1]
    before = snapshot(tmp_path)
    # blind=False: the verdict as it was before T-CL-14 made the card blind.
    card = rec.pending_card(
        rig.run_dir, now_wall_ns=wait.emitted_wall_ns + 90_000_000_000, blind=False
    )
    assert snapshot(tmp_path) == before  # read only
    assert card["waiting"] and card["state"] == "WAIT_HUMAN"
    assert card["reason"] == "scene_reset_required"
    assert card["expected_seq"] == rig.orch.journal.next_seq
    assert card["waited_ms"] == 90_000
    assert card["human_wait_number"] == 1 and card["human_waits_since_forward"] == 1
    last = card["last_episode"]
    assert last["episode_id"] == f"{RUN}.forward.0001"
    assert (last["task_outcome"], last["stop_reason"], last["robot_home"]) == (
        "success",
        "goal_verified",
        "not_attempted",
    )
    assert last["rollout"]["demo"] == "demo_0001"
    assert last["rollout_path"].endswith(f"{GROUP}/{FOLDERS['forward']}/demo_0001")
    assert Path(last["rollout_path"]).is_dir()
    assert set(last["last_frames"]) == {"side", "wrist"}
    contract = card["contract"]
    assert contract["key"] == "stack-plates-initial@1"
    assert contract["status"] == "draft" and contract["unconfirmed"]
    assert "HA-23" in contract["notice"] and "未经用户确认" in contract["notice"]
    assert [p["text"] for p in contract["predicates"]] == [
        "object at source",
        "gripper open",
    ]
    evidence = card["evidence"]
    assert evidence["decision"] == "reset_required"
    assert evidence["failed_predicates"] == ["object_at_source"]
    assert evidence["frames"][0]["file"].startswith("frames/")


def test_the_card_counts_the_waits_and_knows_a_confirmed_contract(tmp_path):
    confirmed = dataclasses.replace(CONTRACT, status="confirmed")
    rig = run(tmp_path, ["ready", "reset_required", "unknown"], contract=confirmed)
    assert rig.orch.run() == "WAIT_HUMAN"
    resume(rig)
    assert rig.orch.run() == "WAIT_HUMAN"
    card = rec.pending_card(rig.run_dir)
    assert card["human_wait_number"] == 2 and card["human_waits_since_forward"] == 2
    assert card["reason"] == "scene_unknown"
    assert card["evidence"]["decision"] == "unknown"
    assert card["evidence"]["unknown_predicates"] == ["object_at_source"]
    assert (
        card["contract"]["unconfirmed"] is False and card["contract"]["notice"] is None
    )


def test_the_card_of_a_run_that_waits_for_nobody(tmp_path):
    rig = run(tmp_path, ["ready", "ready"], episodes=1)
    assert rig.orch.run() == "COMPLETED"
    card = rec.pending_card(rig.run_dir)
    assert not card["waiting"] and card["reason"] is None and card["waited_ms"] is None
    assert card["last_episode"]["episode_id"] == f"{RUN}.forward.0001"
