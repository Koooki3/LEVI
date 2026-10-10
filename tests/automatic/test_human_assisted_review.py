"""Review CL2b (levi2/recon/review-CL2b.md): regression tests for B1 (an
answer that existed before its question), B2 (the provider locking itself),
I1-I4 and the mutants M4, M5b, M6, M6b, M7 and M19 that the first tests
missed."""

import dataclasses
import hashlib
import json
import threading

import pytest
from aeri_harness import RUN, build, committed, config
from test_human_assisted_scene import CONTRACT, TIMEOUT_NS, Camera, person_run

from levi.automatic import cli
from levi.automatic import recorder as rec
from levi.automatic.adapters import human
from levi.automatic.orchestrator import Orchestrator

YES = {"object_at_source": True, "gripper_open": True}


def request(number=9):
    return {
        "request_id": f"{RUN}.forward.0001:scene{number}",
        "run_id": RUN,
        "episode_id": f"{RUN}.forward.0001",
        "target": "initial_state",
    }


def codes(provider):
    return [c for c, _ in provider.drain_notes()]


def bounded(fn, seconds=5.0):
    """Run ``fn`` in a thread; fail (instead of hanging) when it does not
    return in time."""
    out = {}

    def target():
        out["value"] = fn()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "hung (a lock taken twice?)"
    return out.get("value")


# --- B1: an answer that existed before the question is never taken ---------------------------


def test_an_answer_queued_before_the_question_is_unsolicited(tmp_path):
    transport = human.QueueTransport()
    rig = person_run(tmp_path, transport)
    req = request()
    transport.answer(req["request_id"], YES)  # the id is predictable
    ticket = rig.scene.submit(req)
    assert rig.scene.collect(ticket, timeout_ns=10**9) is None
    assert codes(rig.scene) == ["scene_answer_unsolicited"]


def test_an_answer_file_written_before_the_question_is_unsolicited(tmp_path):
    root = tmp_path / "scene"
    req = request()
    folder = root / "answers"
    folder.mkdir(parents=True)
    (folder / "pre.json").write_text(
        json.dumps({"request_id": req["request_id"], "predicates": YES})
    )
    transport = human.FileTransport(root)
    rig = person_run(tmp_path, transport)
    ticket = rig.scene.submit(req)
    assert rig.scene.collect(ticket, timeout_ns=10**9) is None
    assert "scene_answer_unsolicited" in codes(rig.scene)
    # The real question is still answerable, with its nonce.
    question = json.loads(
        (root / "questions" / f"{req['request_id']}.json").read_text()
    )
    human.write_answer(root, question, YES)
    assert (
        json.loads(rig.scene.collect(ticket, timeout_ns=10**9))["decision"] == "ready"
    )


@pytest.mark.parametrize(
    "forged",
    [{"nonce": "guessed"}, {"frames_sha256": "0" * 64}, {"nonce": None}],
)
def test_an_answer_with_the_wrong_nonce_or_frames_is_unsolicited(tmp_path, forged):
    transport = human.QueueTransport()
    rig = person_run(tmp_path, transport)
    req = request()
    ticket = rig.scene.submit(req)
    question = transport.questions[req["request_id"]]
    assert len(question["nonce"]) >= 16
    answer = {**human.answer_for(question, YES), **forged}
    transport.put(answer)
    assert rig.scene.collect(ticket, timeout_ns=10**9) is None
    assert codes(rig.scene) == ["scene_answer_unsolicited"]


def test_two_questions_never_share_a_nonce_and_an_id_is_asked_once(tmp_path):
    transport = human.QueueTransport()
    rig = person_run(tmp_path, transport)
    one = rig.scene.submit(request(1))
    two = rig.scene.submit(request(2))
    nonces = {q["nonce"] for q in transport.questions.values()}
    assert len(nonces) == 2 and one.request_id != two.request_id
    again = rig.scene.submit(request(1))
    assert isinstance(again, bytes)
    assert json.loads(again)["kind"] == "unavailable"


# --- B2: no lock is taken twice --------------------------------------------------------------


def test_a_request_withdrawn_while_its_answer_is_checked_does_not_hang(tmp_path):
    transport = human.QueueTransport()
    rig = person_run(tmp_path, transport)
    req = request()
    ticket = rig.scene.submit(req)
    transport.answer(req["request_id"], YES)
    real = rig.scene._check

    def check_then_withdraw(answer):
        found = real(answer)
        rig.scene.cancel(ticket, "stopped")  # another thread, in effect
        return found

    rig.scene._check = check_then_withdraw
    bounded(rig.scene._take)
    assert codes(rig.scene) == ["scene_answer_unsolicited"]


def test_a_malformed_repeat_of_an_answered_request_is_unsolicited_not_refused(tmp_path):
    """M5b: the first "already answered" check decides, before the content."""
    transport = human.QueueTransport()
    rig = person_run(tmp_path, transport)
    req = request()
    ticket = rig.scene.submit(req)
    question = transport.questions[req["request_id"]]
    transport.put(human.answer_for(question, YES))
    transport.put(human.answer_for(question, {"object_at_source": "yes"}))
    bounded(rig.scene._take)
    assert codes(rig.scene) == ["scene_answer_unsolicited"]
    assert rig.scene.collect(ticket, timeout_ns=10**9) is not None


# --- M4: an answer is claimed by its own request id only ---------------------------------------


def test_an_answer_to_another_request_never_takes_the_open_one(tmp_path):
    transport = human.QueueTransport()
    rig = person_run(tmp_path, transport)
    old = rig.scene.submit(request(1))
    old_question = dict(transport.questions[old.request_id])
    rig.scene.cancel(old, "timeout")
    ticket = rig.scene.submit(request(2))
    transport.put(human.answer_for(old_question, YES))  # the old question's answer
    transport.put({**human.answer_for(old_question, YES), "request_id": "other"})
    assert rig.scene.collect(ticket, timeout_ns=10**9) is None
    assert codes(rig.scene) == ["scene_answer_unsolicited"] * 2
    transport.answer(ticket.request_id, YES)
    assert rig.scene.collect(ticket, timeout_ns=10**9) is not None


# --- I1: the question shows the frames it asks about -----------------------------------------


def test_the_question_names_the_saved_frames_before_it_is_asked(tmp_path):
    transport = human.QueueTransport()
    rig = person_run(tmp_path, transport)
    req = request()
    rig.scene.submit(req)
    question = transport.questions[req["request_id"]]
    (frame,) = question["frames"]
    assert frame["file"].startswith("evidence/frames/") and frame["skipped"] is None
    data = (rig.directory / frame["file"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == frame["sha256"]
    assert question["frames_sha256"] == human.frames_digest(question["frames"])


# --- I2: a frame that was not kept is no evidence ----------------------------------------------


def test_an_unsaved_frame_is_not_evidence_and_never_ready(tmp_path):
    cfg = config(
        reset_strategy="human_assisted",
        scene_check="operator_attested",
        human_scene_timeout_ns=TIMEOUT_NS,
        initial_state=CONTRACT,
        episodes=1,
    )
    rig = build(tmp_path, cfg=cfg, create=False)
    store = rec.EvidenceStore(rig.directory, max_frame_bytes=4)
    rig.scene = human.HumanSceneProvider(
        CONTRACT,
        Camera(),
        human.ScriptedTransport(["ready"]),
        rig.clock,
        RUN,
        wait=rig.clock.advance,
    )
    rig.orch = Orchestrator.create(rig.directory, cfg, evidence=store, **rig.parts())
    assert rig.orch.run() == "WAIT_HUMAN"
    assert committed(rig.orch)[-1][1:] == ("WAIT_HUMAN", "scene_unknown")
    (record,) = rec.evidence_records(rig.directory)
    assert record["provider_decision"] == "unknown"
    assert record["evidence_refs"] == []
    assert record["frame_unsaved"] and record["frames"] == []


def test_without_an_evidence_store_a_persons_check_never_passes(tmp_path):
    cfg = config(
        reset_strategy="human_assisted",
        scene_check="operator_attested",
        initial_state=CONTRACT,
        episodes=1,
    )
    rig = build(tmp_path, cfg=cfg, create=False)
    rig.scene = human.HumanSceneProvider(
        CONTRACT,
        Camera(),
        human.ScriptedTransport(["ready"]),
        rig.clock,
        RUN,
        wait=rig.clock.advance,
    )
    rig.orch = Orchestrator.create(rig.directory, cfg, evidence=None, **rig.parts())
    assert rig.orch.run() == "WAIT_HUMAN"
    assert committed(rig.orch)[-1][2] == "scene_unknown"


# --- I3: only a wait for a reset is a human reset ------------------------------------------------


def test_a_resume_after_a_stop_is_not_a_human_reset(tmp_path):
    from test_human_assisted_mode import READY, human_run, manifest, resume

    rig = human_run(tmp_path, [READY, READY, READY])
    real = rig.sessions.__call__

    def stop_once(state, info, seen=[]):  # noqa: B006 - one-shot latch
        real(state, info)
        if state == "FORWARD_ACTIVE" and not seen:
            seen.append(True)
            rig.orch.stop("stop-1")

    rig.orch.listener = stop_once
    assert rig.orch.run() == "WAIT_HUMAN"
    assert committed(rig.orch)[-1][2] == "operator_stop"
    resume(rig)
    assert rig.orch.run() == "COMPLETED"
    second = manifest(tmp_path)["episodes"][1]
    assert second["preceded_by"] == "none"
    assert second["after_human_resets"] == []


# --- I4: a provider must say it is reachable -----------------------------------------------------


def test_a_provider_without_a_reachability_check_is_refused(tmp_path):
    class Silent:
        provider = "fake"

    cfg = config(reset_strategy="human_assisted")
    problems = cli.scene_provider_problems(cfg, Silent())
    assert problems and problems[0].startswith("E_SCENE_PROVIDER_MISSING")


def test_run_config_needs_a_contract_for_a_persons_check():
    from levi.automatic.orchestrator import ConfigError

    with pytest.raises(ConfigError):
        config(
            reset_strategy="human_assisted",
            scene_check="operator_attested",
            initial_state=None,
        )


# --- M6, M6b, M19: the evidence store ------------------------------------------------------------


def test_every_evidence_file_is_synced_before_its_rename_and_the_folder_after(
    tmp_path, monkeypatch
):
    calls = []
    real_fsync, real_replace = rec.os.fsync, rec.os.replace
    monkeypatch.setattr(
        rec.os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1]
    )
    monkeypatch.setattr(
        rec.os,
        "replace",
        lambda a, b: (calls.append(f"replace {b.name}"), real_replace(a, b))[1],
    )
    real_dir = rec.fsync_dir
    monkeypatch.setattr(
        rec, "fsync_dir", lambda p: (calls.append(f"dir {p.name}"), real_dir(p))[1]
    )
    store = rec.EvidenceStore(tmp_path)
    calls.clear()
    store.write({"request_id": "q-1", "journal_seq": 1}, {"side:a": b"frame"})
    renames = [i for i, c in enumerate(calls) if c.startswith("replace")]
    assert len(renames) == 2  # the frame, then the record
    for index in renames:
        name = calls[index].split()[1]
        assert calls[index - 1] == "fsync", calls
        folder = "frames" if name != "q-1.json" else "evidence"
        assert f"dir {folder}" in calls[index + 1 :], calls


@pytest.mark.parametrize("name", ["../x", "a/b", ".hidden", ""])
def test_an_evidence_id_never_names_a_path(tmp_path, name):
    store = rec.EvidenceStore(tmp_path)
    with pytest.raises(rec.RecorderError):
        store.write({"request_id": name, "journal_seq": 1})
    assert not (tmp_path / "x.json").exists()
    assert rec.evidence_records(tmp_path) == []


# --- M7: no motion token after a stop that does not home -------------------------------------------


def test_no_motion_token_when_a_stop_leaves_the_arm_where_it_is(tmp_path):
    from test_human_assisted_mode import READY, human_run

    rig = human_run(tmp_path, [READY])
    rig.orch.config = dataclasses.replace(
        rig.orch.config, home_after_operator_stop=False
    )
    real = rig.sessions.__call__
    seen = []

    def listener(state, info):
        real(state, info)
        seen.append((state, rig.fence.current))
        if state == "FORWARD_ACTIVE" and len([s for s, _ in seen if s == state]) == 1:
            rig.orch.stop("stop-1")

    rig.orch.listener = listener
    assert rig.orch.run() == "WAIT_HUMAN"
    assert committed(rig.orch)[-1][1:] == ("WAIT_HUMAN", "operator_stop")
    assert not any(c[1] == "SCENE_ASSESS" for c in committed(rig.orch))  # no home
    waits = [token for state, token in seen if state == "WAIT_HUMAN"]
    assert waits == [None]
    assert rig.fence.current is None


# --- the file transport, hardened ---------------------------------------------------------------


def test_the_file_transport_reads_no_link_and_no_fifo(tmp_path):
    import os

    root = tmp_path / "scene"
    transport = human.FileTransport(root)
    transport.reachable()
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"request_id": "x", "predicates": {}}))
    os.symlink(secret, root / "answers" / "link.json")
    os.mkfifo(root / "answers" / "fifo.json")
    found = bounded(transport.take_answers)
    assert len(found) == 2 and all("_refused" in a for a in found)
    with pytest.raises(ValueError):
        transport.ask({"request_id": "../out", "predicates": []})
