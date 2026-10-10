"""The operator's own outcome label next to the automatic verdict (T-CL-14,
dual labels): what a label may say, when it may be written, the waiting
card that asks for it without showing the automatic verdict first, and the
agreement of the two by how the episode ended. The label channel never
touches the run journal, the manifest or a rollout. Fakes only."""

import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_aeri_cli import CONTRACT, as_json, job_text

from levi.automatic import metrics as M
from levi.automatic import modes
from levi.automatic import recorder as rec
from levi.automatic.journal import Journal

ROOT = Path(__file__).resolve().parents[2]
RUN = "r-cli"
MS = 1_000_000


def ep(n, role="forward"):
    return f"{RUN}.{role}.{n:04d}"


def tree(folder) -> dict:
    """Every file under ``folder`` with its bytes."""
    return {
        str(p.relative_to(folder)): p.read_bytes()
        for p in sorted(Path(folder).rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def kept(tmp_path, capsys, reset_mode):
    """A dry run of each reset mode, kept: the first forward episode ended
    (an early stop the fake verifier confirmed), then a scene that is not
    ready (human_assisted waits for a person, the reset policy resets)."""
    root = tmp_path / "rollouts"
    root.mkdir()
    (tmp_path / "initial-state.yaml").write_text(CONTRACT)
    job = tmp_path / "automatic-eval.yaml"
    job.write_text(job_text(root, strategy=reset_mode))
    keep = tmp_path / "kept"
    code, found = as_json(
        capsys,
        "run",
        "--config",
        str(job),
        "--dry-run",
        "--keep",
        str(keep),
        "--scenes",
        "ready,reset_required",
    )
    assert code == 0, found
    human = reset_mode == modes.HUMAN
    assert found["state"] == ("WAIT_HUMAN" if human else "COMPLETED")
    return SimpleNamespace(
        folder=keep, run_dir=keep / ".aeri" / "runs" / RUN, human=human, job=job
    )


# --- what a label says -------------------------------------------------------------------------


def test_an_operator_label_has_four_values_and_appends(tmp_path):
    store = M.LabelStore(tmp_path)
    for value in ("success", "discarded", "unclear", "failure"):
        store.add("operator_label", ep(1), value, by="op-1", supersede=True)
    # The latest is the current value; every earlier one stays on file.
    assert store.latest("operator_label") == {ep(1): "failure"}
    lines = store.lines("operator_label")
    assert [line["value"] for line in lines] == [
        "success",
        "discarded",
        "unclear",
        "failure",
    ]
    assert [line["supersedes"] for line in lines] == [None, 1, 2, 3]
    # Without supersede a second label is still refused (the store's rule).
    with pytest.raises(M.LabelRefused):
        store.add("operator_label", ep(1), "success", by="op-1")
    # The other kinds and the other subject keep their two values.
    for kind in ("posthoc_verdict", "adjudicated_ground_truth"):
        with pytest.raises(M.LabelRefused):
            store.add(kind, ep(2), "unclear", by="op-1")
    with pytest.raises(M.LabelRefused):
        store.add("operator_label", ep(2), "discarded", subject="initial_state", by="o")
    assert sorted(p.name for p in (tmp_path / "labels").iterdir()) == [
        ".lock",
        "operator_label.jsonl",
    ]


def test_discarded_and_unclear_are_never_truth(tmp_path):
    store = M.LabelStore(tmp_path)
    store.add("operator_label", ep(1), "success", by="op-1")
    store.add("operator_label", ep(2), "failure", by="op-1")
    store.add("operator_label", ep(3), "discarded", by="op-1")
    assert store.truth() == {ep(1): "success", ep(2): "failure"}
    # A later unclear takes the earlier success out of the truth: the
    # current value is the operator's, not the last decided one.
    store.add("operator_label", ep(1), "unclear", by="op-1", supersede=True)
    assert store.truth() == {ep(2): "failure"}
    store.add("adjudicated_ground_truth", ep(1), "failure", by="rev-1")
    assert store.truth() == {ep(1): "failure", ep(2): "failure"}
    assert store.truth(rule="adjudicated") == {ep(1): "failure"}


@pytest.mark.parametrize("by", ["op-1\n", "Ada Lovelace", "ada@example.org", ""])
def test_the_principal_is_an_opaque_id(tmp_path, by):
    with pytest.raises(M.LabelRefused):
        M.LabelStore(tmp_path).add("operator_label", ep(1), "success", by=by)
    assert not (tmp_path / "labels" / "operator_label.jsonl").exists()


# --- when a label may be written ------------------------------------------------------------------


@pytest.mark.mode_matrix("metrics:agreement")
def test_only_an_ended_forward_episode_takes_a_label(kept, reset_mode):
    before = tree(kept.folder)
    for episode, why in (
        (ep(9), "has not ended"),
        ("not-an-episode", "not an episode id"),
        ("other-run.forward.0001", "has not ended"),
    ):
        with pytest.raises(M.LabelRefused, match=why):
            M.label_operator(kept.run_dir, episode, "success", by="op-1")
    if not kept.human:
        # The reset policy's episode is not the policy under evaluation.
        with pytest.raises(M.LabelRefused, match="forward"):
            M.label_operator(kept.run_dir, ep(1, "reset"), "success", by="op-1")
    assert tree(kept.folder) == before
    record = M.label_operator(kept.run_dir, ep(1), "failure", by="op-1", note="x")
    assert record["kind"] == "operator_label" and record["value"] == "failure"
    after = tree(kept.folder)
    # The journal, the manifest, the rollouts and the session files are
    # byte for byte what they were: only the label file (and its lock) is new.
    changed = {k for k in after if before.get(k) != after[k]}
    labels = ".aeri/runs/r-cli/labels"
    assert changed == {f"{labels}/operator_label.jsonl", f"{labels}/.lock"}
    # A correction is appended; the report reads the latest.
    M.label_operator(kept.run_dir, ep(1), "success", by="op-2")
    found = M.report(
        Journal.read(kept.run_dir).events, labels=M.LabelStore(kept.run_dir)
    )
    total = found["agreement"]["total"]
    assert total["judged"] == 1 and total["agree"] == 1
    assert found["agreement"]["by_ended_by"]["early_stop"]["judged"] == 1
    # One episode: an interval, no point estimate.
    assert total["agreement"]["rate"] is None and total["small_sample"]
    assert total["agreement"]["wilson95"] == [0.207, 1.0]
    assert "agreement" in found["comparable"]


def test_an_episode_still_running_takes_no_label(kept_single):
    run_dir = kept_single
    journal = run_dir / "state_journal.jsonl"
    events = Journal.read(run_dir).events
    start = next(
        i
        for i, e in enumerate(events)
        if e.record == "committed" and e.to_state == "FORWARD_ACTIVE"
    )
    lines = journal.read_bytes().split(b"\n")
    # The journal as it stood while the episode ran (a valid prefix).
    journal.write_bytes(b"\n".join(lines[: start + 1]) + b"\n")
    with pytest.raises(M.LabelRefused, match="has not ended"):
        M.label_operator(run_dir, ep(1), "success", by="op-1")
    assert not (run_dir / "labels").exists()


@pytest.fixture
def kept_single(tmp_path, capsys):
    root = tmp_path / "rollouts"
    root.mkdir()
    (tmp_path / "initial-state.yaml").write_text(CONTRACT)
    job = tmp_path / "automatic-eval.yaml"
    job.write_text(job_text(root))
    keep = tmp_path / "kept"
    code, _ = as_json(
        capsys, "run", "--config", str(job), "--dry-run", "--keep", str(keep)
    )
    assert code == 0
    return keep / ".aeri" / "runs" / RUN


def test_a_run_without_a_journal_takes_no_label(tmp_path):
    with pytest.raises(M.LabelRefused, match="has not ended"):
        M.label_operator(tmp_path, ep(1), "success", by="op-1")
    with pytest.raises(M.LabelRefused):
        M.label_operator(tmp_path, ep(1), "maybe", by="op-1")
    assert not (tmp_path / "labels").exists()


# --- agreement, by how the episode ended ------------------------------------------------------------


def result(stop, verification):
    outcome = {"verified": "success", "contradicted": "failure"}.get(
        verification, "unknown"
    )
    return SimpleNamespace(
        task_outcome=outcome,
        stop_reason=stop,
        goal_verification=verification,
        scene_reset="unknown",
        rollout=SimpleNamespace(sealed="complete"),
    )


def journal(*episodes):
    events = []
    for n, stop, verification in episodes:
        events.append(
            SimpleNamespace(
                record="committed",
                from_state="FORWARD_FINALIZE",
                to_state="ROBOT_HOME",
                reason=stop,
                episode_id=ep(n),
                episode_result=result(stop, verification),
                mono_ns=n * MS,
                clock_domain="host-mono:test",
            )
        )
    return events


def test_agreement_is_split_by_how_the_episode_ended(tmp_path):
    episodes = [
        # budget: 12 episodes, operator decided on 10 of them.
        *[(n, "horizon_exhausted", "contradicted") for n in range(1, 8)],  # 7
        *[(n, "horizon_exhausted", "verified") for n in range(8, 11)],  # 3
        (11, "horizon_exhausted", "undecided"),
        (12, "horizon_exhausted", "unavailable"),
        # early stops
        (13, "goal_verified", "verified"),
        (14, "goal_verified", "verified"),
        # the operator ended it
        (15, "operator_stop", "unavailable"),
        # a fault, and a reason the mapping does not know
        (16, "safety_stop", "unavailable"),
        (17, "orchestrator_crash", "unavailable"),
    ]
    store = M.LabelStore(tmp_path)
    labels = {
        **{n: "failure" for n in range(1, 7)},  # agree 6
        7: "success",  # missed success (automatic failure)
        8: "success",
        9: "success",  # agree 2
        10: "failure",  # false success
        11: "success",  # undecided, apart
        12: "failure",  # none, apart
        13: "success",  # agree
        14: "unclear",
        15: "discarded",
        16: "failure",  # none
    }
    for n, value in labels.items():
        store.add("operator_label", ep(n), value, by="op-1")
    found = M.report(journal(*episodes), labels=store)["agreement"]
    assert found["label_kind"] == "operator_label"
    assert found["min_n"] == M.AGREEMENT_MIN_N == 10
    budget = found["by_ended_by"]["budget"]
    assert budget["episodes"] == 12 and budget["unlabelled"] == 0
    assert budget["matrix"] == {
        "success": {"success": 2, "failure": 1, "undecided": 1, "none": 0},
        "failure": {"success": 1, "failure": 6, "undecided": 0, "none": 1},
    }
    assert (budget["judged"], budget["agree"]) == (10, 8)
    assert (budget["undecided"], budget["none"]) == (1, 1)
    # Ten decided pairs: the point estimate is given, with its interval.
    assert not budget["small_sample"]
    assert budget["agreement"]["rate"] == 0.8
    assert budget["agreement"]["wilson95"] == [0.49, 0.943]
    assert budget["false_success"]["n"] == 1 and budget["false_success"]["of"] == 7
    assert budget["missed_success"]["n"] == 1 and budget["missed_success"]["of"] == 3
    early = found["by_ended_by"]["early_stop"]
    assert (early["judged"], early["agree"], early["unclear"]) == (1, 1, 1)
    assert early["small_sample"] and early["agreement"]["rate"] is None
    assert early["agreement"]["wilson95"] == [0.207, 1.0]
    stop = found["by_ended_by"]["operator_stop"]
    assert (stop["episodes"], stop["discarded"], stop["judged"]) == (1, 1, 0)
    assert stop["agreement"]["wilson95"] is None
    unknown = found["by_ended_by"]["unknown"]
    assert (unknown["episodes"], unknown["unlabelled"], unknown["none"]) == (2, 1, 1)
    total = found["total"]
    assert total["episodes"] == 17
    assert (total["judged"], total["agree"]) == (11, 9)
    assert (total["discarded"], total["unclear"], total["unlabelled"]) == (1, 1, 1)
    assert (total["undecided"], total["none"]) == (1, 2)
    assert total["agreement"]["rate"] == 0.818
    # The strata add up to the total.
    for key in ("episodes", "judged", "agree", "unlabelled", "undecided", "none"):
        assert sum(found["by_ended_by"][k][key] for k in M.ENDED_BY) == total[key]
    # Reset episodes never count; without labels every episode is unlabelled.
    bare = M.report(journal(*episodes))["agreement"]["total"]
    assert bare["unlabelled"] == 17 and bare["judged"] == 0


def test_how_an_episode_ended(tmp_path):
    assert M.ended_by("horizon_exhausted") == "budget"
    assert M.ended_by("goal_verified") == "early_stop"
    assert M.ended_by("operator_stop") == "operator_stop"
    for other in ("safety_stop", "policy_error", "home_failed", None, "new_reason"):
        assert M.ended_by(other) == "unknown"
    assert [M.automatic_of(v) for v in ("verified", "contradicted")] == [
        "success",
        "failure",
    ]
    assert M.automatic_of("undecided") == "undecided"
    assert M.automatic_of("unavailable") == M.automatic_of(None) == "none"


# --- the waiting card ------------------------------------------------------------------------------


@pytest.fixture
def waiting(tmp_path, capsys):
    root = tmp_path / "rollouts"
    root.mkdir()
    (tmp_path / "initial-state.yaml").write_text(CONTRACT)
    job = tmp_path / "automatic-eval.yaml"
    job.write_text(job_text(root, strategy=modes.HUMAN))
    keep = tmp_path / "kept"
    code, found = as_json(
        capsys,
        "run",
        "--config",
        str(job),
        "--dry-run",
        "--keep",
        str(keep),
        "--scenes",
        "ready,reset_required",
    )
    assert code == 0 and found["state"] == "WAIT_HUMAN"
    return keep


def leaves(value, key=None):
    """Every string in a card, except the list of values a label may take."""
    if isinstance(value, dict):
        return [x for k, v in value.items() for x in leaves(v, k)]
    if isinstance(value, list) and key != "values":
        return [x for v in value for x in leaves(v, key)]
    return [value] if isinstance(value, str) else []


def test_the_waiting_card_hides_the_verdict_until_the_operator_labels(waiting):
    run_dir = waiting / ".aeri" / "runs" / RUN
    before = tree(waiting)
    card = rec.pending_card(run_dir)
    assert tree(waiting) == before  # read only
    block = card["operator_label"]
    assert block["episode_id"] == ep(1) and block["ended_by"] == "early_stop"
    assert block["current"] is None and not block["labelled"] and block["labels"] == 0
    assert block["automatic_verdict"] is None
    assert block["automatic_verdict_hidden"] == "hidden_until_labelled"
    assert block["agrees"] is None and block["values"] == list(M.OPERATOR_VALUES)
    # Blind: nothing in the card says what the run decided.
    blind = rec.pending_card(run_dir, blind=True)
    last = blind["last_episode"]
    assert last["task_outcome"] is None and last["goal_verification"] is None
    assert last["verdict_hidden"] == "hidden_until_labelled"
    verdicts = {"success", "failure", "verified", "contradicted", "undecided"}
    assert not verdicts & set(leaves(blind)), leaves(blind)
    assert json.dumps(blind)  # plain JSON
    # The default keeps the card as it was (T-CL-04) apart from the new block.
    assert card["last_episode"]["task_outcome"] == "success"
    M.label_operator(run_dir, ep(1), "failure", by="op-1")
    after = rec.pending_card(run_dir, blind=True)["operator_label"]
    assert after["current"] == "failure" and after["labelled"] and after["labels"] == 1
    assert after["automatic_verdict"] == {
        "verdict": "success",
        "task_outcome": "success",
        "goal_verification": "verified",
    }
    assert after["automatic_verdict_hidden"] is None and after["agrees"] is False
    shown = rec.pending_card(run_dir, blind=True)["last_episode"]
    assert shown["task_outcome"] == "success" and "verdict_hidden" not in shown
    M.label_operator(run_dir, ep(1), "unclear", by="op-1")
    block = rec.pending_card(run_dir)["operator_label"]
    assert block["current"] == "unclear" and block["agrees"] is None


def test_an_unreadable_label_file_keeps_the_card_blind(waiting):
    run_dir = waiting / ".aeri" / "runs" / RUN
    (run_dir / "labels").mkdir()
    (run_dir / "labels" / "operator_label.jsonl").write_text("not json\n")
    block = rec.pending_card(run_dir, blind=True)["operator_label"]
    assert block["labels_unreadable"] and block["current"] is None
    assert block["automatic_verdict"] is None
    with pytest.raises(M.LabelRefused):
        M.label_operator(run_dir, ep(1), "success", by="op-1")


def test_a_card_before_any_forward_episode_has_no_label_block(tmp_path, capsys):
    root = tmp_path / "rollouts"
    root.mkdir()
    (tmp_path / "initial-state.yaml").write_text(CONTRACT)
    job = tmp_path / "automatic-eval.yaml"
    job.write_text(job_text(root, strategy=modes.HUMAN))
    keep = tmp_path / "kept"
    _, found = as_json(
        capsys,
        "run",
        "--config",
        str(job),
        "--dry-run",
        "--keep",
        str(keep),
        "--scenes",
        "reset_required",
    )
    assert found["state"] == "WAIT_HUMAN"
    card = rec.pending_card(keep / ".aeri" / "runs" / RUN, blind=True)
    assert card["operator_label"] is None and card["last_episode"] is None


# --- concurrent writers and a writer killed half-way ----------------------------------------------------


WRITER = """
import sys
from levi.automatic import metrics as M
store = M.LabelStore(sys.argv[1])
for i in range(int(sys.argv[3])):
    store.add("operator_label", f"r-cli.forward.{i % 5 + 1:04d}",
              ("success", "failure", "discarded", "unclear")[i % 4],
              by=sys.argv[2], supersede=True)
"""


def test_concurrent_writers_never_tear_or_lose_a_label(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", WRITER, str(tmp_path), f"op-{k}", "25"], env=env
        )
        for k in range(4)
    ]
    for proc in procs:
        assert proc.wait(timeout=120) == 0
    lines = M.LabelStore(tmp_path).lines("operator_label")
    assert len(lines) == 100
    assert sorted({line["by"] for line in lines}) == ["op-0", "op-1", "op-2", "op-3"]
    # Under the lock each writer counts the earlier labels of its episode.
    for n in range(1, 6):
        mine = [line for line in lines if line["episode_id"] == ep(n)]
        assert [line["supersedes"] for line in mine] == [None, *range(1, len(mine))]
    data = (tmp_path / "labels" / "operator_label.jsonl").read_bytes()
    assert data.endswith(b"\n") and data.count(b"\n") == 100


KILLED = """
import os, signal, sys
from levi.automatic import metrics as M
store = M.LabelStore(sys.argv[1])
store.add("operator_label", "r-cli.forward.0001", "success", by="op-1")
real = os.write
def half(fd, data):
    real(fd, data[: len(data) // 2])
    os.kill(os.getpid(), signal.SIGKILL)
os.write = half
store.add("operator_label", "r-cli.forward.0001", "failure", by="op-1", supersede=True)
"""


def test_a_writer_killed_half_way_loses_only_its_own_label(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(KILLED), str(tmp_path)],
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == -signal.SIGKILL, proc.stderr[-2000:]
    path = tmp_path / "labels" / "operator_label.jsonl"
    assert not path.read_bytes().endswith(b"\n")  # torn
    store = M.LabelStore(tmp_path)
    # The torn half is no label: the earlier one is still the current value.
    assert store.latest("operator_label") == {ep(1): "success"}
    # The dead writer's lock is gone with it; the next label isolates the
    # torn bytes and starts on its own line.
    store.add("operator_label", ep(1), "unclear", by="op-2", supersede=True)
    assert [line["value"] for line in store.lines("operator_label")] == [
        "success",
        "unclear",
    ]
    (torn,) = (tmp_path / "labels" / "torn").iterdir()
    assert torn.read_bytes() and b"\n" not in torn.read_bytes()
    assert path.read_bytes().endswith(b"\n")
