"""T-CL-03: a person as the scene provider (``adapters/human.py``,
``reset.scene_check: operator_attested``). The person answers predicates on
frames captured for the check, after the resume; "cannot tell" is unknown;
no answer in time is unavailable; a repeated or unrequested answer is
dropped (``E_UNSOLICITED``); nothing a person can send says "ready" by
itself, and only a ready verdict of the arbitration starts an episode."""

import json
import threading
import time

import pytest
from aeri_harness import RUN, build, committed, config

from levi.automatic import cli
from levi.automatic import scene_assessment as sa
from levi.automatic.adapters import human
from levi.automatic.orchestrator import Orchestrator

CONTRACT = sa.InitialStateContract(
    contract_id="stack-plates-initial",
    contract_version="1",
    required=("object_at_source", "gripper_open"),
    optional=("lid_closed",),
    min_evidence_refs=1,
)
TIMEOUT_NS = 600 * 10**9


class Camera:
    """Frames that change at every capture; records what the journal said
    when it was asked (the capture must follow the resume)."""

    def __init__(self, views=("side",)):
        self.views = views
        self.shots = []
        self.orch = None
        self.fail = False

    def __call__(self):
        if self.fail:
            raise OSError("camera gone")
        events = self.orch.journal.events if self.orch else []
        last = next((e for e in reversed(events) if e.record == "committed"), None)
        self.shots.append(
            (len(events), self.orch.state if self.orch else None, last and last.reason)
        )
        n = len(self.shots)
        return {view: f"frame {view} {n}".encode() for view in self.views}


def person_run(tmp_path, transport, *, views=("side",), contract=CONTRACT, episodes=1):
    cfg = config(
        reset_strategy="human_assisted",
        scene_check="operator_attested",
        human_scene_timeout_ns=TIMEOUT_NS,
        initial_state=contract,
        episodes=episodes,
    )
    rig = build(tmp_path, cfg=cfg, create=False)
    camera = Camera(views)
    rig.scene = human.HumanSceneProvider(
        contract, camera, transport, rig.clock, RUN, wait=rig.clock.advance
    )
    rig.orch = Orchestrator.create(rig.directory, cfg, **rig.parts())
    camera.orch = rig.orch
    rig.camera = camera
    return rig


def resume(rig, command="op-1"):
    found = rig.orch.resume(
        command,
        expected_seq=rig.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    )
    assert found.ok, found
    return found


def notes(rig):
    return [
        (e.note.code, e.note.detail)
        for e in rig.orch.journal.events
        if e.record == "note"
    ]


def started(rig) -> int:
    return sum(1 for t in committed(rig.orch) if t[1] == "FORWARD_ACTIVE")


# --- the answers ------------------------------------------------------------------------------


def test_the_frames_are_captured_after_the_resume(tmp_path):
    rig = person_run(tmp_path, human.ScriptedTransport(["reset_required", "ready"]))
    assert rig.orch.run() == "WAIT_HUMAN"
    assert committed(rig.orch)[-1][2] == "scene_reset_required"
    found = resume(rig)
    assert rig.orch.run() == "COMPLETED"
    first, second = rig.camera.shots[:2]
    # The first check: after the preflight; the second: after the resume,
    # its preflight, in VERIFY_INITIAL (never before the person's resume).
    assert first[1:] == ("VERIFY_INITIAL", "preflight_passed")
    assert second[1:] == ("VERIFY_INITIAL", "preflight_passed")
    assert second[0] > found.sequence_no
    resumed = [e for e in rig.orch.journal.events if e.reason == "human_resumed"]
    assert resumed and resumed[-1].sequence_no < second[0]
    assert started(rig) == 1


def test_the_assessment_is_the_persons_on_the_captured_frames(tmp_path):
    transport = human.ScriptedTransport(["ready"])
    rig = person_run(tmp_path, transport)
    question = None

    real_ask = transport.ask

    def ask(q):
        nonlocal question
        question = q
        real_ask(q)

    transport.ask = ask
    assert rig.orch.run() == "COMPLETED"
    assert question["contract"] == {
        "id": "stack-plates-initial",
        "version": "1",
        "status": "draft",
    }
    assert [p["text"] for p in question["predicates"]] == [
        "object at source",
        "gripper open",
        "lid closed",
    ]
    assert [p["required"] for p in question["predicates"]] == [True, True, False]
    assert question["frames"][0]["ref"].startswith("side:")
    # The question is gone once answered.
    assert transport.questions == {}


def build_message(rig, transport, values):
    """Ask once (target initial_state) and return the parsed assessment."""
    from levi.domain import aeri

    request = {
        "request_id": f"{RUN}.forward.0001:scene9",
        "run_id": RUN,
        "episode_id": f"{RUN}.forward.0001",
        "target": "initial_state",
    }
    ticket = rig.scene.submit(request)
    transport.answer(request["request_id"], values)
    raw = rig.scene.collect(ticket, timeout_ns=10**9)
    return aeri.parse(raw, "scene", specs=CONTRACT.spec())


@pytest.mark.parametrize(
    ("values", "decision", "failed", "unknown"),
    [
        ({"object_at_source": True, "gripper_open": True}, "ready", [], []),
        (
            {"object_at_source": False, "gripper_open": None},
            "reset_required",
            ["object_at_source"],
            ["gripper_open"],
        ),
        # "cannot tell" is unknown, never ready
        (
            {"object_at_source": None, "gripper_open": True},
            "unknown",
            [],
            ["object_at_source"],
        ),
        # an optional predicate never decides
        (
            {"object_at_source": True, "gripper_open": True, "lid_closed": False},
            "ready",
            [],
            [],
        ),
    ],
)
def test_the_decision_follows_from_the_answers(
    tmp_path, values, decision, failed, unknown
):
    transport = human.QueueTransport()
    rig = person_run(tmp_path, transport)
    message = build_message(rig, transport, values)
    assert (
        message.decision,
        message.failed_predicates,
        message.unknown_predicates,
    ) == (
        decision,
        failed,
        unknown,
    )
    assert message.provider == "human" and message.model_name is None
    assert message.evidence_refs and message.evidence_refs[0].kind == "frame"
    if decision == "unknown":
        assert message.unknown_reason == "insufficient_evidence"


@pytest.mark.parametrize(
    "answer",
    [
        {"decision": "ready"},  # not a predicate answer at all
        {"ready": True},
        {"object_at_source": True},  # a required predicate left out
        {"object_at_source": "true", "gripper_open": True},  # text, not a value
        {"object_at_source": 1, "gripper_open": True},  # a number is not true
        {"object_at_source": True, "gripper_open": True, "scene_ok": True},
    ],
)
def test_a_person_answers_predicates_only(tmp_path, answer):
    transport = human.QueueTransport()
    rig = person_run(tmp_path, transport)
    request = {
        "request_id": f"{RUN}.forward.0001:scene9",
        "run_id": RUN,
        "episode_id": f"{RUN}.forward.0001",
        "target": "initial_state",
    }
    ticket = rig.scene.submit(request)
    transport.answer(request["request_id"], answer)
    assert rig.scene.collect(ticket, timeout_ns=10**9) is None
    assert [c for c, _ in rig.scene.drain_notes()] == ["scene_answer_refused"]
    # The question stays open: a proper answer is still taken.
    transport.answer(
        request["request_id"], {"object_at_source": True, "gripper_open": True}
    )
    assert rig.scene.collect(ticket, timeout_ns=10**9) is not None


def test_a_decision_key_beside_the_predicates_is_refused(tmp_path):
    transport = human.QueueTransport()
    rig = person_run(tmp_path, transport)
    request = {
        "request_id": f"{RUN}.forward.0001:scene9",
        "run_id": RUN,
        "episode_id": f"{RUN}.forward.0001",
        "target": "initial_state",
    }
    ticket = rig.scene.submit(request)
    transport.answer(
        request["request_id"],
        {"object_at_source": False, "gripper_open": True},
        decision="ready",
    )
    assert rig.scene.collect(ticket, timeout_ns=10**9) is None
    assert rig.scene.drain_notes()[0][0] == "scene_answer_refused"


def test_a_repeated_or_unrequested_answer_is_dropped(tmp_path):
    transport = human.QueueTransport()
    rig = person_run(tmp_path, transport)
    request = {
        "request_id": f"{RUN}.forward.0001:scene9",
        "run_id": RUN,
        "episode_id": f"{RUN}.forward.0001",
        "target": "initial_state",
    }
    yes = {"object_at_source": True, "gripper_open": True}
    ticket = rig.scene.submit(request)
    transport.answer(request["request_id"], yes)
    transport.answer(request["request_id"], {**yes, "gripper_open": False})
    transport.answer("never-asked", yes)
    raw = rig.scene.collect(ticket, timeout_ns=10**9)
    assert json.loads(raw)["decision"] == "ready"  # the first answer only
    # Once answered, the request id is spent.
    transport.answer(request["request_id"], yes)
    found = rig.scene.drain_notes()
    assert [c for c, _ in found] == ["scene_answer_unsolicited"] * 3
    assert all(d.startswith("E_UNSOLICITED") for _, d in found)
    assert rig.scene.collect(ticket, timeout_ns=10**9) is None


# --- in the run ---------------------------------------------------------------------------------


def test_cannot_tell_waits_for_a_person_and_never_starts(tmp_path):
    rig = person_run(tmp_path, human.ScriptedTransport(["unknown", "unknown"]))
    assert rig.orch.run() == "WAIT_HUMAN"
    assert committed(rig.orch)[-1][1:] == ("WAIT_HUMAN", "scene_unknown")
    resume(rig)
    assert rig.orch.run() == "WAIT_HUMAN"
    assert started(rig) == 0 and rig.robot.motions == []


def test_no_answer_in_time_is_unavailable_and_waits_for_a_person(tmp_path):
    transport = human.ScriptedTransport([None])  # nobody answers
    rig = person_run(tmp_path, transport)
    before = rig.clock.now()
    assert rig.orch.run() == "WAIT_HUMAN"
    assert committed(rig.orch)[-1][1:] == ("WAIT_HUMAN", "scene_unknown")
    assert rig.clock.now() - before >= TIMEOUT_NS
    assert [c for c, _ in notes(rig)] == ["scene_timeout"]
    # The question was withdrawn; an answer now is dropped.
    (request_id,) = [r for r, _ in transport.withdrawn]
    assert transport.questions == {}
    transport.answer(request_id, {"object_at_source": True, "gripper_open": True})
    resume(rig)
    assert rig.orch.run() == "COMPLETED"
    codes = [c for c, _ in notes(rig)]
    assert "scene_answer_unsolicited" in codes
    assert started(rig) == 1


def test_a_stop_ends_a_persons_check_at_once(tmp_path):
    class Stopping(human.QueueTransport):
        polls = 0

        def take_answers(self):
            self.polls += 1
            if self.polls == 3:
                assert rig.orch.stop("stop-1").ok
            return super().take_answers()

    transport = Stopping()
    rig = person_run(tmp_path, transport)
    before = rig.clock.now()
    assert rig.orch.run() == "WAIT_HUMAN"
    assert committed(rig.orch)[-1][1:] == ("WAIT_HUMAN", "operator_stop")
    assert rig.clock.now() - before < TIMEOUT_NS // 10
    assert transport.withdrawn and transport.withdrawn[0][1] == "stopped"
    assert started(rig) == 0


def test_all_true_on_frames_that_miss_a_view_is_not_ready(tmp_path):
    contract = sa.InitialStateContract(
        contract_id="stack-plates-initial",
        contract_version="1",
        required=("object_at_source", "gripper_open"),
        preferred_views=("side", "wrist"),
        min_evidence_refs=2,
    )
    rig = person_run(
        tmp_path,
        human.ScriptedTransport(["ready"]),
        views=("side",),  # the wrist camera gave nothing
        contract=contract,
    )
    assert rig.orch.run() == "WAIT_HUMAN"
    assert committed(rig.orch)[-1][1:] == ("WAIT_HUMAN", "scene_unknown")
    assert any(c == "scene_missing_view" for c, _ in notes(rig))
    assert started(rig) == 0


@pytest.mark.parametrize("fault", ["capture", "transport"])
def test_a_failing_capture_or_transport_is_unavailable_never_a_pass(tmp_path, fault):
    transport = human.ScriptedTransport(["ready"])
    rig = person_run(tmp_path, transport)
    if fault == "capture":
        rig.camera.fail = True
    else:

        def broken(question):
            raise OSError("disk full")

        transport.ask = broken
    assert rig.orch.run() == "WAIT_HUMAN"
    assert committed(rig.orch)[-1][1:] == ("WAIT_HUMAN", "scene_unknown")
    assert any(c == "scene_check_failed" for c, _ in notes(rig))
    assert started(rig) == 0


def test_an_answer_from_another_thread_while_the_run_waits(tmp_path):
    transport = human.QueueTransport()
    cfg = config(
        reset_strategy="human_assisted",
        scene_check="operator_attested",
        human_scene_timeout_ns=TIMEOUT_NS,
        initial_state=CONTRACT,
        episodes=1,
    )
    rig = build(tmp_path, cfg=cfg, create=False)

    def wait(ns):
        time.sleep(0.002)
        rig.clock.advance(ns)

    rig.scene = human.HumanSceneProvider(
        CONTRACT, Camera(), transport, rig.clock, RUN, wait=wait
    )
    rig.orch = Orchestrator.create(rig.directory, cfg, **rig.parts())

    def person():
        for _ in range(2000):
            if transport.questions:
                (request_id,) = list(transport.questions)
                transport.answer(
                    request_id, {"object_at_source": True, "gripper_open": True}
                )
                transport.answer(  # a double click
                    request_id, {"object_at_source": True, "gripper_open": True}
                )
                return
            time.sleep(0.001)

    thread = threading.Thread(target=person)
    thread.start()
    assert rig.orch.run() == "COMPLETED"
    thread.join(5)
    assert started(rig) == 1
    assert [c for c, _ in notes(rig)].count("scene_answer_unsolicited") <= 1


# --- the file transport -------------------------------------------------------------------------


def test_the_file_transport_round_trip(tmp_path):
    root = tmp_path / "scene"
    transport = human.FileTransport(root)
    assert transport.reachable() is True
    transport.ask({"request_id": "q-1", "predicates": []})
    assert (
        json.loads((root / "questions" / "q-1.json").read_text())["request_id"] == "q-1"
    )
    assert oct((root / "questions").stat().st_mode & 0o777) == "0o700"
    human.write_answer(
        root,
        {"request_id": "q-1", "nonce": "n", "frames_sha256": "f"},
        {"a": True},
    )
    # A writer killed half-way leaves only its hidden temporary file.
    (root / "answers" / ".q-1.json.123.tmp").write_text('{"request_id": "q-1", "pre')
    (root / "answers" / "bad.json").write_text('{"request_id": "x", "request_id": "y"}')
    found = transport.take_answers()
    assert {
        "request_id": "q-1",
        "nonce": "n",
        "frames_sha256": "f",
        "predicates": {"a": True},
    } in found
    assert any("_refused" in a for a in found)
    assert transport.take_answers() == []  # taken once
    assert len(list((root / "answers" / "taken").iterdir())) == 1
    assert len(list((root / "answers" / "refused").iterdir())) == 1
    transport.withdraw("q-1", "answered")
    assert not (root / "questions" / "q-1.json").exists()


def test_a_new_provider_withdraws_questions_left_by_a_dead_process(tmp_path):
    root = tmp_path / "scene"
    human.FileTransport(root).ask({"request_id": "old", "predicates": []})
    rig = build(tmp_path, cfg=config(), create=False)
    human.HumanSceneProvider(
        CONTRACT, Camera(), human.FileTransport(root), rig.clock, RUN
    )
    assert list((root / "questions").glob("*.json")) == []


def test_an_unwritable_transport_is_not_reachable(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    found = human.FileTransport(blocker / "scene").reachable()
    assert isinstance(found, str) and found
    cfg = config(reset_strategy="human_assisted", scene_check="operator_attested")
    rig = build(tmp_path, cfg=config(), create=False)
    provider = human.HumanSceneProvider(
        CONTRACT, Camera(), human.FileTransport(blocker / "scene"), rig.clock, RUN
    )
    problems = cli.scene_provider_problems(cfg, provider)
    assert problems and problems[0].startswith("E_SCENE_PROVIDER_MISSING")


def test_a_person_cannot_attest_without_a_contract():
    with pytest.raises(ValueError):
        human.HumanSceneProvider(None, Camera(), human.QueueTransport(), None, RUN)


# --- the dry run --------------------------------------------------------------------------------


def test_a_dry_run_with_a_scripted_person(tmp_path, capsys):
    from test_human_assisted_contracts import HUMAN, write

    text = HUMAN.replace(
        "strategy: human_assisted",
        "strategy: human_assisted\n  scene_check: operator_attested",
    )
    path = write(tmp_path, text)
    code = cli.main(["validate", "--config", str(path), "--dry-run", "--json"])
    assert code == 0, capsys.readouterr().out
    capsys.readouterr()
    code = cli.main(["run", "--config", str(path), "--dry-run", "--json"])
    found = json.loads(capsys.readouterr().out)
    assert code == 0 and found["state"] == "COMPLETED"
    assert found["metrics"]["autonomous"]["forward_episodes"] == 3
    code = cli.main(
        [
            "run",
            "--config",
            str(path),
            "--dry-run",
            "--json",
            "--scenes",
            "reset_required",
        ]
    )
    found = json.loads(capsys.readouterr().out)
    assert found["state"] == "WAIT_HUMAN" and found["robot"]["motions"] == 0


@pytest.mark.parametrize("request_id", ["../escape", "a/b", ".hidden", ""])
def test_an_answer_file_is_never_written_outside_its_folder(tmp_path, request_id):
    with pytest.raises(ValueError):
        human.write_answer(
            tmp_path / "scene",
            {"request_id": request_id, "nonce": "n", "frames_sha256": "f"},
            {"a": True},
        )
    assert not (tmp_path / "escape").exists()
    assert list(tmp_path.rglob("*.json")) == []
