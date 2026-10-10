"""T-CL-02: the human-assisted ("policy evaluation only") mode writes no reset
session file, no reset rollout and no reset folder; who put the scene back
is in the journal, the manifest (``after_human_resets``) and the next
forward episode's metadata (``eval.aeri.preceded_by``); a human-assisted
run without a scene check that can answer is refused at launch
(``E_SCENE_PROVIDER_MISSING``, design X2 G2/G6)."""

import json

import pytest
from aeri_harness import RUN, build, committed, config
from aeri_world import FOLDERS

from levi.automatic import cli
from levi.automatic.adapters import events as ev
from levi.automatic.recorder import (
    MANIFEST,
    SESSION_DIR,
    RolloutRecorder,
    SessionFiles,
)

GROUP = "aeri"


class Spy(SessionFiles):
    """The session files, plus the motion token seen at every state."""

    def __init__(self, fence, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fence = fence
        self.seen = []

    def __call__(self, state, info):
        self.seen.append((state, self.fence.current))
        super().__call__(state, info)


def human_run(tmp_path, scenes, *, strategy="human_assisted", episodes=2):
    cfg = config(
        reset_strategy=strategy,
        episodes=episodes,
        forward_folder=FOLDERS["forward"],
        reset_folder=FOLDERS["reset"],
    )
    run_dir = tmp_path / ".aeri" / "runs" / RUN
    recorder = RolloutRecorder(
        tmp_path, run_id=RUN, run_dir=run_dir, group=GROUP, texts={}
    )
    rig = build(tmp_path, cfg=cfg, recorder=recorder, create=False)
    rig.scene = ev.FakeSceneAssessor(scenes, rig.clock, RUN)
    settled = [{"step": 8, "event_type": "object_settled"}]
    rig.events = ev.FakeEventStream(
        {"forward": settled, "reset": settled}, rig.clock, RUN
    )
    rig.sessions = Spy(
        rig.fence,
        tmp_path,
        run_id=RUN,
        group=GROUP,
        folders=cli.session_folders(cfg),
    )
    from levi.automatic.orchestrator import Orchestrator

    rig.orch = Orchestrator.create(
        rig.directory, cfg, listener=rig.sessions, **rig.parts()
    )
    return rig


def resume(rig, command="op-1", wait_ns=7_000_000_000):
    rig.clock.advance(wait_ns)
    found = rig.orch.resume(
        command,
        expected_seq=rig.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    )
    assert found.ok, found
    return found


def manifest(tmp_path):
    return json.loads((tmp_path / ".aeri" / "runs" / RUN / MANIFEST).read_text())


def metadata(tmp_path, number):
    path = tmp_path / GROUP / FOLDERS["forward"] / f"demo_{number:04d}"
    return json.loads((path / "metadata.json").read_text())


READY, RESET = {"decision": "ready"}, {"decision": "reset_required"}


def test_a_person_puts_the_scene_back_and_the_data_says_so(tmp_path):
    # Episode 1 starts on a ready scene; after it the scene needs a reset:
    # a person puts it back, resumes, and the system checks it again.
    rig = human_run(tmp_path, [READY, RESET, READY])
    assert rig.orch.run() == "WAIT_HUMAN"
    assert committed(rig.orch)[-1] == (
        "SCENE_ASSESS",
        "WAIT_HUMAN",
        "scene_reset_required",
    )
    wait_seq = rig.orch.journal.events[-1].sequence_no
    resume(rig)
    assert rig.orch.run() == "COMPLETED"
    rig.orch.close()
    rig.recorder.close()

    # No reset episode, no reset state, no reset folder, no reset session.
    states = {s for t in committed(rig.orch) for s in t[:2]}
    assert not states & {"RESET_ACTIVE", "RESET_VERIFY", "RESET_FINALIZE"}
    assert not (tmp_path / GROUP / FOLDERS["reset"]).exists()
    sessions = sorted(p.name for p in (tmp_path / SESSION_DIR).iterdir())
    assert sessions == [f"{GROUP}__{FOLDERS['forward']}.json"]
    found = manifest(tmp_path)
    assert {e["role"] for e in found["episodes"]} == {"forward"}
    assert found["folders"] == {"forward": FOLDERS["forward"]}

    first, second = found["episodes"]
    assert (first["preceded_by"], first["after_human_resets"]) == ("none", [])
    assert second["preceded_by"] == "human_reset"
    assert len(second["after_human_resets"]) == 1
    reset = second["after_human_resets"][0]
    assert (
        reset["wait_seq"],
        reset["wait_ms"],
        reset["principal_id"],
        reset["reason"],
    ) == (wait_seq, 7000, "operator", "scene_reset_required")
    resume_line = rig.orch.journal.events[reset["resume_seq"]]
    assert resume_line.record == "committed" and resume_line.reason == "human_resumed"
    assert resume_line.authority.command_id == "op-1"
    assert metadata(tmp_path, 1)["eval"]["aeri"]["preceded_by"] == "none"
    assert metadata(tmp_path, 2)["eval"]["aeri"]["preceded_by"] == "human_reset"
    # The verdict is still not an operator label.
    assert metadata(tmp_path, 2)["eval"]["outcome"] == "unlabeled"


def test_no_motion_token_while_a_person_is_waited_for(tmp_path):
    rig = human_run(tmp_path, [READY, RESET, {"decision": "unknown"}, READY])
    assert rig.orch.run() == "WAIT_HUMAN"
    resume(rig)
    assert rig.orch.run() == "WAIT_HUMAN"  # unknown after the resume: a person again
    resume(rig, "op-2")
    assert rig.orch.run() == "COMPLETED"
    waits = [token for state, token in rig.sessions.seen if state == "WAIT_HUMAN"]
    assert len(waits) == 2 and all(token is None for token in waits)
    still = {
        state: token
        for state, token in rig.sessions.seen
        if state in ("PREFLIGHT", "VERIFY_INITIAL", "SCENE_ASSESS", "COMPLETED")
    }
    assert all(token is None for token in still.values())
    # Two waits, two resumes before episode 2.
    second = manifest(tmp_path)["episodes"][1]
    assert [r["reason"] for r in second["after_human_resets"]] == [
        "scene_reset_required",
        "scene_unknown",
    ]


def test_with_a_reset_policy_both_session_files_and_preceded_by_reset_policy(
    tmp_path,
):
    rig = human_run(tmp_path, [READY, RESET, READY], strategy="single_reset_policy")
    assert rig.orch.run() == "COMPLETED"
    sessions = sorted(p.name for p in (tmp_path / SESSION_DIR).iterdir())
    assert len(sessions) == 2
    forward = [e for e in manifest(tmp_path)["episodes"] if e["role"] == "forward"]
    assert [e["preceded_by"] for e in forward] == ["none", "reset_policy"]
    assert metadata(tmp_path, 2)["eval"]["aeri"]["preceded_by"] == "reset_policy"


def test_a_person_after_a_failed_reset_is_the_last_word(tmp_path):
    # The reset policy did not make the scene ready: a person did.
    rig = human_run(
        tmp_path,
        [READY, RESET, RESET, READY],
        strategy="single_reset_policy",
    )
    assert rig.orch.run() == "WAIT_HUMAN"
    resume(rig)
    assert rig.orch.run() == "COMPLETED"
    second = [e for e in manifest(tmp_path)["episodes"] if e["role"] == "forward"][1]
    assert second["preceded_by"] == "human_reset"
    assert second["after_resets"] and second["after_human_resets"]


def test_session_files_refuse_unknown_roles(tmp_path):
    with pytest.raises(ValueError):
        SessionFiles(tmp_path, run_id=RUN, group=GROUP, folders={"reset": "x"})
    with pytest.raises(ValueError):
        SessionFiles(
            tmp_path, run_id=RUN, group=GROUP, folders={"forward": "x", "other": "y"}
        )


def test_a_human_assisted_dry_run_leaves_one_session_file(tmp_path, capsys):
    from test_human_assisted_contracts import HUMAN, write

    path = write(tmp_path, HUMAN)
    keep = tmp_path / "kept"
    code = cli.main(
        [
            "run",
            "--config",
            str(path),
            "--dry-run",
            "--keep",
            str(keep),
            "--json",
            "--episodes",
            "1",
        ]
    )
    found = json.loads(capsys.readouterr().out)
    assert code == 0 and found["state"] == "COMPLETED"
    assert sorted(p.name for p in (keep / SESSION_DIR).iterdir()) == [
        "aeri__stack__r-ha.json"
    ]
    assert sorted(p.name for p in (keep / "aeri").iterdir()) == ["stack__r-ha"]


# --- G6: no scene check that can answer, no launch -----------------------------------------------


class Provider:
    def __init__(self, provider="fake", reachable=True):
        self.provider = provider
        self._reachable = reachable

    def reachable(self):
        if isinstance(self._reachable, Exception):
            raise self._reachable
        return self._reachable


@pytest.mark.parametrize(
    ("check", "provider", "refused"),
    [
        ("provider", None, True),
        ("provider", Provider(reachable=False), True),
        ("provider", Provider(reachable="vllm: connection refused"), True),
        ("provider", Provider(reachable=OSError("down")), True),
        ("provider", Provider(), False),
        ("provider", "fake", False),
        ("operator_attested", None, True),
        ("operator_attested", "fake", True),
        ("operator_attested", Provider("fake"), True),
        ("operator_attested", Provider("human"), False),
        ("operator_attested", Provider("human", reachable=False), True),
        ("operator_attested", "human", False),
    ],
)
def test_a_human_assisted_run_needs_a_scene_check_that_answers(
    check, provider, refused
):
    cfg = config(reset_strategy="human_assisted", scene_check=check)
    problems = cli.scene_provider_problems(cfg, provider)
    assert bool(problems) is refused
    assert all(p.startswith("E_SCENE_PROVIDER_MISSING") for p in problems)


def test_the_reset_policy_mode_keeps_its_launch_rules(tmp_path):
    cfg = config(reset_strategy="single_reset_policy")
    assert cli.scene_provider_problems(cfg, None) == []


def test_validate_refuses_a_real_human_assisted_run_with_the_code(tmp_path, capsys):
    from test_human_assisted_contracts import HUMAN, write

    path = write(tmp_path, HUMAN)
    code = cli.main(["validate", "--config", str(path), "--json"])
    found = json.loads(capsys.readouterr().out)
    assert code == 2 and "E_SCENE_PROVIDER_MISSING" in found["error"]
    code = cli.main(["validate", "--config", str(path), "--json", "--dry-run"])
    assert code == 0


def test_a_double_click_resumes_once_and_counts_one_human_reset(tmp_path):
    rig = human_run(tmp_path, [READY, RESET, READY])
    assert rig.orch.run() == "WAIT_HUMAN"
    seq = rig.orch.journal.next_seq
    first = resume(rig)
    again = rig.orch.resume(
        "op-1", expected_seq=seq, environment_handled=True, health_rechecked=True
    )
    assert again.ok and again.code == "repeated" and again.repeated
    late = rig.orch.resume(
        "op-2", expected_seq=seq, environment_handled=True, health_rechecked=True
    )
    assert not late.ok and late.code in ("stale_sequence", "not_waiting")
    assert first.code == "resumed"
    assert rig.orch.run() == "COMPLETED"
    second = manifest(tmp_path)["episodes"][1]
    assert len(second["after_human_resets"]) == 1
