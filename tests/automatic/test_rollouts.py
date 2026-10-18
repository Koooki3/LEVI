"""AERI rollout sets (``levi.automatic.rollouts``): synthetic journals and
fake demo folders. Covers rounds -> forward/reverse sets -> subtasks ->
attempts, retries, an abandoned and a locked round, an incomplete demo, a
demo that matches no attempt, idempotence, and no write without --write."""

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from levi.automatic import cli, rollouts

RUN = "aeri-t-r01"
GROUP = "g"
TASK = "pick the eggplant into the white plate"
FORWARD = f"pick_eggplant__{RUN}__subtask"
REVERSE = f"pick_eggplant__{RUN}__reverse"
WALL0 = 1_792_259_000_000_000_000


class _Journal:
    """Builds a journal the way the orchestrator writes it (only the fields
    the generator reads)."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.lines = []
        self.seq = 0
        self.wall = WALL0
        self.add({"record": "run_header", "header": {"reset_mode": "subtask_loop"}})

    def add(self, event: dict, *, seconds: float = 1.0) -> None:
        self.seq += 1
        self.wall += int(seconds * 1e9)
        base = {
            "run_id": RUN,
            "sequence_no": self.seq,
            "emitted_wall_ns": self.wall,
            "episode_id": None,
            "episode_result": None,
            "note": None,
            "to_state": None,
            "reason": None,
        }
        base.update(event)
        self.lines.append(base)

    def note(self, code: str, detail) -> None:
        text = detail if isinstance(detail, str) else json.dumps(detail)
        self.add({"record": "note", "note": {"code": code, "detail": text}})

    def sidecar(self, name: str, value: dict) -> str:
        folder = self.run_dir / "subtasks"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(json.dumps(value))
        return f"subtasks/{name}"

    def release(self, number: int) -> None:
        self.note(
            "subtask_round_released",
            {"by": "terminal", "round": number, "round_id": f"{RUN}:round{number}"},
        )

    def plan(self, number: int, loop: str, items: list) -> None:
        file = self.sidecar(
            f"{self.seq + 1:06d}-r{number:03d}-{loop}-plan.json",
            {"loop": loop, "round": number, "subtasks": items},
        )
        self.note(
            "subtask_plan",
            {
                "loop": loop,
                "n": len(items),
                "plan_file": file,
                "round": number,
                "task": TASK if loop == "forward" else f"undo: {TASK}",
            },
        )

    def episode(
        self,
        number: int,
        loop: str,
        index: int,
        attempt: int,
        episode: str,
        *,
        check=True,
        sealed="complete",
        stop="horizon_exhausted",
        to_state="SCENE_ASSESS",
        reason="subtask_continue",
    ) -> None:
        instruction = (
            "put the purple eggplant into the white plate"
            if loop == "forward"
            else "put the purple eggplant into the blue plate"
        )
        self.note(
            "subtask_started",
            {
                "attempt": attempt,
                "episode_id": episode,
                "index": index,
                "instruction": instruction,
                "loop": loop,
                "round": number,
            },
        )
        role = "forward" if loop == "forward" else "reset"
        self.add(
            {
                "record": "committed",
                "episode_id": episode,
                "episode_role": role,
                "to_state": "FORWARD_ACTIVE" if role == "forward" else "RESET_ACTIVE",
                "reason": "subtask_dispatched",
            }
        )
        demo = f"demo_{int(episode.rsplit('.', 1)[1]):04d}"
        self.add(
            {
                "record": "committed",
                "episode_id": episode,
                "episode_role": role,
                "to_state": to_state,
                "reason": reason,
                "episode_result": {
                    "stop_reason": stop,
                    "rollout": {"demo": demo, "sealed": sealed, "task_folder": "x"},
                    "label_kind": "autonomous_verdict",
                    "task_outcome": "unknown",
                },
            },
            seconds=20.0,
        )
        if check is None:
            return
        file = self.sidecar(
            f"{self.seq + 1:06d}-subtask_check.json",
            {"request_id": f"check-{self.seq + 1}", "decision": check},
        )
        self.note(
            "subtask_check",
            {
                "attempt": attempt,
                "episode_id": episode,
                "file": file,
                "index": index,
                "loop": loop,
                "round": number,
                "value": check,
            },
        )

    def write(self, *, torn: str = "") -> None:
        data = "".join(json.dumps(e) + "\n" for e in self.lines) + torn
        (self.run_dir / rollouts.JOURNAL).write_text(data)


def _demo(root: Path, folder: str, name: str, episode, *, frames=200, aeri=None):
    path = root / GROUP / folder / name
    path.mkdir(parents=True)
    block = aeri if aeri is not None else {"episode_id": episode, "run_id": RUN}
    meta = {"frame_count": frames, "eval": {"outcome": "unlabeled", "aeri": block}}
    if name.startswith("incomplete_"):
        meta["eval"]["abort_reason"] = "fr3_fault"
    (path / "metadata.json").write_text(json.dumps(meta))
    return path


PLAN_F = [
    {
        "index": 0,
        "instruction": "put the purple eggplant into the white plate",
        "pick": "purple_eggplant_1",
        "place": "white_plate_1",
    }
]
PLAN_R = [
    {
        "index": 0,
        "instruction": "put the purple eggplant into the blue plate",
        "pick": "purple_eggplant_1",
        "place": "blue_plate_1",
    }
]


@pytest.fixture
def run(tmp_path):
    """Round 1: forward 1 subtask (done), reverse 3 attempts (2 retries).
    Round 2: forward done, reverse abandoned (human_resumed).
    Round 3: forward locked (safety_stop), its recording incomplete.
    Plus a reverse demo whose episode id no attempt names."""
    root = tmp_path / "models"
    run_dir = root / ".aeri" / "runs" / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "plan.json").write_text(
        json.dumps(
            {
                "plan": {
                    "recording": {"group": GROUP, "rollout_root": str(root)},
                    "run": {
                        "run_id": RUN,
                        "subtask_task": TASK,
                        "subtask_forward_folder": FORWARD,
                        "subtask_reverse_folder": REVERSE,
                    },
                    "subtask": {"vlm": {"provider_mode": "external"}},
                }
            }
        )
    )
    j = _Journal(run_dir)
    j.release(1)
    j.plan(1, "forward", PLAN_F)
    j.episode(1, "forward", 0, 1, f"{RUN}.forward.0001", reason="subtask_forward_done")
    j.note(
        "subtask_loop_done",
        {"loop": "forward", "outcome": "done", "round": 1, "subtasks": 1},
    )
    j.plan(1, "reverse", PLAN_R)
    j.episode(1, "reverse", 0, 1, f"{RUN}.reset.0001", check=False)
    j.note("subtask_retry", {"attempt": 2, "index": 0, "loop": "reverse", "round": 1})
    j.episode(1, "reverse", 0, 2, f"{RUN}.reset.0002", check=False)
    j.note("subtask_retry", {"attempt": 3, "index": 0, "loop": "reverse", "round": 1})
    j.episode(1, "reverse", 0, 3, f"{RUN}.reset.0003", check=True)
    j.note(
        "subtask_loop_done",
        {"loop": "reverse", "outcome": "done", "round": 1, "subtasks": 1},
    )
    j.release(2)
    j.plan(2, "forward", PLAN_F)
    j.episode(2, "forward", 0, 1, f"{RUN}.forward.0002", reason="subtask_forward_done")
    j.note(
        "subtask_loop_done",
        {"loop": "forward", "outcome": "done", "round": 2, "subtasks": 1},
    )
    j.plan(2, "reverse", PLAN_R)
    j.episode(
        2,
        "reverse",
        0,
        1,
        f"{RUN}.reset.0004",
        check=False,
        to_state="WAIT_HUMAN",
        reason="subtask_retry_limit",
    )
    j.add({"record": "committed", "to_state": "PREFLIGHT", "reason": "human_resumed"})
    j.note(
        "subtask_round_abandoned",
        {"phase": "reverse", "round": 2, "why": "human_resumed"},
    )
    j.release(3)
    j.plan(3, "forward", PLAN_F)
    j.episode(
        3,
        "forward",
        0,
        1,
        f"{RUN}.forward.0003",
        check=None,
        sealed="incomplete",
        stop="safety_stop",
        to_state="FAULT_LOCKED",
        reason="safety_stop",
    )
    j.note("motion_refused", "step 13: action was rewritten")
    j.write()

    _demo(root, FORWARD, "demo_0001", f"{RUN}.forward.0001")
    _demo(root, FORWARD, "demo_0002", f"{RUN}.forward.0002")
    _demo(root, FORWARD, "incomplete_0003", f"{RUN}.forward.0003", frames=0)
    for n in range(1, 5):
        _demo(root, REVERSE, f"demo_{n:04d}", f"{RUN}.reset.{n:04d}")
    _demo(root, REVERSE, "demo_0009", f"{RUN}.reset.0009")
    (root / GROUP / REVERSE / "task_description.txt").write_text("undo")
    # A folder of another run is never read.
    _demo(
        root, "pick_eggplant__aeri-t-r02__reverse", "demo_0001", "aeri-t-r02.reset.0001"
    )
    labels = run_dir / "labels"
    labels.mkdir()
    (labels / "round_label.jsonl").write_text(
        json.dumps(
            {
                "kind": "round_label",
                "record": "label",
                "round_id": f"{RUN}:round1",
                "value": "success",
                "by": "op1",
                "source": "terminal",
                "members": {"forward": [f"{RUN}.forward.0001"], "reverse": []},
            }
        )
        + "\n"
    )
    return SimpleNamespace(root=root, run_dir=run_dir)


def _files(folder: Path) -> dict:
    return {
        str(p.relative_to(folder)): p.read_bytes()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def test_rounds_sets_subtasks_attempts(run):
    found = rollouts.build_rollouts(run.run_dir, run.root)
    assert found["schema"] == rollouts.SCHEMA == "levi.aeri.rollouts.v1"
    assert found["run_id"] == RUN
    assert found["task"] == TASK
    assert found["mode"] == "external"
    assert found["reset_mode"] == "subtask_loop"
    assert found["generated_from"]["events"] > 0
    assert len(found["generated_from"]["journal_sha256"]) == 64
    assert [r["round"] for r in found["rollouts"]] == [1, 2, 3]
    first = found["rollouts"][0]
    assert first["round_id"] == f"{RUN}:round1"
    assert first["released_at"].endswith("+00:00")
    assert first["outcome"] == "forward_done"
    forward = first["sets"]["forward"]
    assert forward["outcome"] == "done"
    assert forward["plan_file"].startswith("subtasks/")
    assert len(forward["subtasks"]) == 1
    sub = forward["subtasks"][0]
    assert (sub["pick"], sub["place"]) == ("purple_eggplant_1", "white_plate_1")
    assert len(sub["attempts"]) == 1
    attempt = sub["attempts"][0]
    assert attempt["demo"] == f"{GROUP}/{FORWARD}/demo_0001"
    assert attempt["frames"] == 200
    assert attempt["stop_reason"] == "horizon_exhausted"
    assert attempt["seconds"] == 20.0
    assert attempt["started_at"] and attempt["ended_at"]
    reverse = first["sets"]["reverse"]["subtasks"]
    assert len(reverse) == 1
    tries = reverse[0]["attempts"]
    assert [a["attempt"] for a in tries] == [1, 2, 3]
    assert [a["check"]["value"] for a in tries] == [False, False, True]
    assert [a["demo"] for a in tries] == [
        f"{GROUP}/{REVERSE}/demo_{n:04d}" for n in (1, 2, 3)
    ]


def test_check_is_the_vlm_judgement_and_labels_only_where_they_exist(run):
    found = rollouts.build_rollouts(run.run_dir, run.root)
    attempt = found["rollouts"][0]["sets"]["forward"]["subtasks"][0]["attempts"][0]
    assert attempt["check"]["by"] == "vlm"
    assert attempt["check"]["value"] is True
    assert attempt["check"]["request"].startswith("check-")
    for found_round in found["rollouts"]:
        for entry in found_round["sets"].values():
            for sub in (entry or {}).get("subtasks", []):
                for item in sub["attempts"]:
                    assert "outcome" not in item and "label" not in item
                    assert "operator_label" not in item
    assert found["rollouts"][0]["operator_label"] == {
        "value": "success",
        "by": "op1",
        "source": "terminal",
    }
    assert "operator_label" not in found["rollouts"][1]


def test_operator_label_of_an_attempt_comes_from_labels(run):
    (run.run_dir / "labels" / "operator_label.jsonl").write_text(
        json.dumps(
            {
                "kind": "operator_label",
                "episode_id": f"{RUN}.forward.0002",
                "subject": "task_outcome",
                "value": "failure",
            }
        )
        + "\n"
    )
    found = rollouts.build_rollouts(run.run_dir, run.root)
    attempt = found["rollouts"][1]["sets"]["forward"]["subtasks"][0]["attempts"][0]
    assert attempt["operator_label"] == "failure"


def test_abandoned_round(run):
    second = rollouts.build_rollouts(run.run_dir, run.root)["rollouts"][1]
    assert second["outcome"] == "abandoned"
    assert second["abandoned"] == {"phase": "reverse", "why": "human_resumed"}
    assert second["sets"]["forward"]["outcome"] == "done"
    assert second["sets"]["reverse"]["outcome"] == "abandoned"
    attempt = second["sets"]["reverse"]["subtasks"][0]["attempts"][0]
    assert attempt["demo"] == f"{GROUP}/{REVERSE}/demo_0004"


def test_locked_round_and_incomplete_demo(run):
    third = rollouts.build_rollouts(run.run_dir, run.root)["rollouts"][2]
    assert third["outcome"] == "locked"
    assert third["locked"]["reason"] == "safety_stop"
    assert third["sets"]["forward"]["outcome"] == "locked"
    assert third["sets"]["reverse"] is None
    attempt = third["sets"]["forward"]["subtasks"][0]["attempts"][0]
    assert attempt["demo"] is None
    assert attempt["incomplete"]["folder"] == f"{GROUP}/{FORWARD}/incomplete_0003"
    assert "fr3_fault" in attempt["incomplete"]["why"]
    assert attempt["stop_reason"] == "safety_stop"
    assert attempt["check"] is None
    assert [n["code"] for n in third["notes"]] == ["motion_refused"]


def test_unmatched_demo_goes_to_problems(run):
    found = rollouts.build_rollouts(run.run_dir, run.root)
    assert found["unmatched_demos"] == [
        {"episode_id": f"{RUN}.reset.0009", "demo": f"{GROUP}/{REVERSE}/demo_0009"}
    ]
    kinds = [p["kind"] for p in found["problems"]]
    assert kinds == ["demo_without_attempt"]
    # The other run's folder is never read.
    assert "aeri-t-r02" not in json.dumps(found)


def test_attempt_without_demo_is_a_problem_not_a_guess(run):
    target = run.root / GROUP / REVERSE / "demo_0002" / "metadata.json"
    target.write_text(
        json.dumps({"eval": {"aeri": {"episode_id": f"{RUN}.reset.0042"}}})
    )
    found = rollouts.build_rollouts(run.run_dir, run.root)
    tries = found["rollouts"][0]["sets"]["reverse"]["subtasks"][0]["attempts"]
    assert tries[1]["demo"] is None
    problems = {(p["kind"], p.get("episode_id")) for p in found["problems"]}
    assert ("attempt_without_demo", f"{RUN}.reset.0002") in problems
    assert ("demo_without_attempt", f"{RUN}.reset.0042") in problems


def test_episode_id_stored_as_a_python_repr(run):
    target = run.root / GROUP / REVERSE / "demo_0001" / "metadata.json"
    target.write_text(
        json.dumps(
            {
                "frame_count": 150,
                "eval": {"aeri": repr({"episode_id": f"{RUN}.reset.0001"})},
            }
        )
    )
    found = rollouts.build_rollouts(run.run_dir, run.root)
    attempt = found["rollouts"][0]["sets"]["reverse"]["subtasks"][0]["attempts"][0]
    assert attempt["demo"] == f"{GROUP}/{REVERSE}/demo_0001"
    assert attempt["frames"] == 150
    assert rollouts.episode_of(
        {"eval": {"aeri": {"episode_id": "'a.reset.0001'"}}}
    ) == ("a.reset.0001")
    # Never eval: an expression is not a literal.
    assert rollouts.episode_of({"eval": {"aeri": "__import__('os')"}}) is None


def test_torn_last_line_ignored_and_bad_line_reported(run):
    path = run.run_dir / rollouts.JOURNAL
    clean = rollouts.build_rollouts(run.run_dir, run.root)
    path.write_bytes(path.read_bytes() + b'{"record": "no')
    torn = rollouts.build_rollouts(run.run_dir, run.root)
    assert torn["rollouts"] == clean["rollouts"]
    assert torn["generated_from"]["events"] == clean["generated_from"]["events"]
    path.write_bytes(b"not json\n" + path.read_bytes())
    bad = rollouts.build_rollouts(run.run_dir, run.root)
    assert {"kind": "journal_line_unreadable", "detail": "line 1"} in bad["problems"]


def test_build_is_pure_and_write_is_idempotent(run):
    before = _files(run.root)
    first = rollouts.build_rollouts(run.run_dir, run.root)
    assert rollouts.build_rollouts(run.run_dir, run.root) == first
    assert _files(run.root) == before
    path = rollouts.write_rollouts(run.run_dir, run.root)
    assert path == run.run_dir / "rollouts.json"
    data = path.read_bytes()
    assert json.loads(data) == first
    assert rollouts.write_rollouts(run.run_dir, run.root).read_bytes() == data
    after = _files(run.root)
    assert set(after) - set(before) == {f".aeri/runs/{RUN}/rollouts.json"}
    assert {k: v for k, v in after.items() if k in before} == before


def test_load_or_build(run):
    built = rollouts.load_or_build(run.run_dir, run.root)
    assert not (run.run_dir / "rollouts.json").exists()
    path = rollouts.write_rollouts(run.run_dir, run.root)
    kept = json.loads(path.read_text())
    kept["marker"] = "from file"
    path.write_text(json.dumps(kept))
    journal = run.run_dir / rollouts.JOURNAL
    mtime = journal.stat().st_mtime_ns
    os.utime(path, ns=(mtime + 10**9, mtime + 10**9))
    assert rollouts.load_or_build(run.run_dir, run.root)["marker"] == "from file"
    # A journal newer than the file: built again in memory, file untouched.
    os.utime(journal, ns=(mtime + 2 * 10**9, mtime + 2 * 10**9))
    again = rollouts.load_or_build(run.run_dir, run.root)
    assert "marker" not in again and again == built
    assert json.loads(path.read_text())["marker"] == "from file"


@pytest.mark.mode_matrix(
    "cli:rollouts", modes=("single_reset_policy", "human_assisted")
)
def test_cli_writes_nothing_without_write(run, capsys):
    before = _files(run.root)
    code = cli.main(
        ["rollouts", "--run-dir", str(run.run_dir), "--rollout-root", str(run.root)]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "round 1" in out and "reverse (done): 1 subtask(s)" in out
    assert "3 attempt(s)" in out and "problems: 1" in out
    assert _files(run.root) == before
    # The default root is the run's own (plan / launch.json, else <root>).
    assert cli.main(["rollouts", "--run-dir", str(run.run_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == RUN
    assert _files(run.root) == before
    assert cli.main(["rollouts", "--run-dir", str(run.run_dir), "--write"]) == 0
    assert "written:" in capsys.readouterr().out
    assert (run.run_dir / "rollouts.json").is_file()


def test_cli_refuses_a_folder_without_journal(tmp_path, capsys):
    assert cli.main(["rollouts", "--run-dir", str(tmp_path)]) == cli.EXIT_REFUSED


def test_scripts_aeri_forwards_rollouts(run, capsys):
    path = Path(__file__).resolve().parents[2] / "scripts" / "aeri.py"
    spec = importlib.util.spec_from_file_location("aeri_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["rollouts", "--run-dir", str(run.run_dir)]) == 0
    assert "round 3" in capsys.readouterr().out
    assert not (run.run_dir / "rollouts.json").exists()


class _Orch:
    def __init__(self):
        self.notes = []
        self.journal = SimpleNamespace(note=self._note)

    def _note(self, code, detail, *, authority):
        self.notes.append((code, detail, authority))

    def _auth(self, kind="orchestrator"):
        return {"principal_kind": kind}


def test_keep_rollouts_at_close_writes_the_manifest(run):
    from levi.automatic import fr3_run

    orch = _Orch()
    assert fr3_run.keep_rollouts(orch, run.run_dir, run.root) is None
    assert (run.run_dir / "rollouts.json").is_file()
    assert orch.notes == []


def test_keep_rollouts_failure_is_a_note_and_never_raises(run, monkeypatch):
    from levi.automatic import fr3_run

    def broken(*_args):
        raise RuntimeError("disk full")

    monkeypatch.setattr(rollouts, "write_rollouts", broken)
    orch = _Orch()
    assert fr3_run.keep_rollouts(orch, run.run_dir, run.root) is None
    assert [(c, d) for c, d, _ in orch.notes] == [
        ("rollouts_write_failed", "disk full")
    ]
    # Even a journal that cannot take the note does not raise.
    orch.journal = SimpleNamespace(note=broken)
    assert fr3_run.keep_rollouts(orch, run.run_dir, run.root) is None


def test_load_or_build_rebuilds_after_a_later_label(run):
    """A label written after rollouts.json (labels/*.jsonl, not the journal)
    makes the file stale, or the labelled round would stay hidden."""
    path = rollouts.write_rollouts(run.run_dir, run.root)
    kept = json.loads(path.read_text())
    kept["marker"] = "from file"
    path.write_text(json.dumps(kept))
    mtime = (run.run_dir / rollouts.JOURNAL).stat().st_mtime_ns
    os.utime(path, ns=(mtime + 10**9, mtime + 10**9))
    assert rollouts.load_or_build(run.run_dir, run.root)["marker"] == "from file"
    labels = run.run_dir / "labels"
    labels.mkdir(exist_ok=True)
    later = labels / "round_label.jsonl"
    later.write_text("")
    os.utime(later, ns=(mtime + 2 * 10**9, mtime + 2 * 10**9))
    assert "marker" not in rollouts.load_or_build(run.run_dir, run.root)
