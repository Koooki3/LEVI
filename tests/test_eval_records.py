"""Evaluation records: a person's session and an agent's full-dataset task are
measured from the same annotation files with the same metrics."""

import json
import time
from pathlib import Path

import pytest

from levi.annotations import status, vocabulary
from levi.eval import metrics, record


def dataset(root, lengths, fps=10.0):
    (root / "meta").mkdir(parents=True)
    (root / "meta/info.json").write_text(json.dumps({"fps": fps}))
    (root / "meta/episodes.jsonl").write_text(
        "\n".join(
            json.dumps({"episode_index": i, "length": n}) for i, n in enumerate(lengths)
        )
    )


def subtask(start, to, content, subtask_id=None, outcome=None, run=None):
    levi = {}
    if subtask_id:
        levi["subtask_id"] = subtask_id
    if outcome:
        levi["outcome"] = outcome
    if run:
        levi["origin"] = {"kind": "agent", "run_id": run}
    return {
        "role": "assistant",
        "content": content,
        "style": "subtask",
        "timestamp": start,
        "to": to,
        "camera": None,
        "levi": levi or None,
    }


def save(folder, episode, atoms):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"episode_{episode:06d}.json").write_text(
        json.dumps({"episode_index": episode, "atoms": atoms})
    )


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Two copies of one 2-episode dataset (11 and 21 frames at 10 fps)."""
    roots, folders = {}, {}
    for name in ("plates_human", "plates_agentVLM"):
        roots[name] = tmp_path / name
        dataset(roots[name], [11, 21])
        folders[name] = tmp_path / "annotations" / name
    monkeypatch.setattr(record, "dataset_root", lambda name: roots[name])
    monkeypatch.setattr(record, "annotations_dir", lambda name: folders[name])
    monkeypatch.setattr(record, "eval_dir", lambda: tmp_path / "eval")
    from levi import catalog

    monkeypatch.setattr(catalog, "datasets", lambda: {name: {} for name in roots})
    return tmp_path, folders


def test_coverage_counts_seconds_frames_gaps_and_overlaps():
    segs = metrics.segments(
        [
            subtask(0.0, 0.4, "approach"),
            subtask(0.4, None, "grasp"),  # runs to the next start: 0.7
            subtask(0.7, 0.8, "lift"),
            subtask(0.75, 0.9, "overlapping"),
        ],
        last=1.0,
    )
    m = metrics.episode_metrics(segs, frames=11, fps=10.0, vocab=set())
    assert m["coverage_seconds"] == pytest.approx(0.9)
    # frames 0.0..0.8 covered (9 of 11); 0.9 and 1.0 are not
    assert m["coverage_frames"] == pytest.approx(9 / 11)
    assert m["overlaps"] == 1 and m["gaps"] == []  # 0.1 s at the end is no gap


def test_semantics_distinguish_vocabulary_labels_from_free_text():
    episodes = {
        0: {
            "frames": 11,
            "fps": 10.0,
            "segments": metrics.segments(
                [
                    subtask(0, 0.5, "move over the green plate", "approach", "success"),
                    subtask(0.5, 1.0, "grasp", "grasp", "success"),
                ],
                1.0,
            ),
        }
    }
    s = metrics.summarize(episodes, {"approach", "grasp", "place"})["semantics"]
    assert s["with_subtask_id"] == 1.0 and s["vocabulary_used"] == 2
    assert s["with_outcome"] == 1.0 and s["label_only"] == 0.5


def test_a_recorded_session_counts_only_episodes_confirmed_while_it_ran(workspace):
    tmp, folders = workspace
    human = folders["plates_human"]
    status.confirm(human, 1, True)  # before the session: not counted
    session = record.start("plates_human")
    assert record.start("plates_human") == session, "starting twice resumes"
    save(human, 0, [subtask(0, 0.5, "reach", "approach"), subtask(0.5, 1.0, "lift")])
    status.confirm(human, 0, True)
    vocabulary.write(human, [{"id": "approach"}, {"id": "grasp"}])
    # The agent copy annotated the same episode, for the side-by-side table.
    save(folders["plates_agentVLM"], 0, [subtask(0, 0.6, "reach", "approach")])
    result = record.stop("plates_human")
    assert result["episodes"] == 1 and result["block"]["confirmed"] == [0]
    path = tmp / "eval" / f"plates_human_human_{record.stamp(time.time())}.md"
    assert result["path"] == str(path)
    text = path.read_text()
    assert "确认完成子任务标注的 episode 数 | 1 / 2" in text
    assert "plates_agentVLM" in text, "the sibling copy is compared"
    assert record.session("plates_human") is None
    # A second session in the same hour is appended, never overwritten.
    record.start("plates_human")
    record.stop("plates_human")
    assert path.read_text().count("### 1. 概况") == 2


def test_stopping_without_a_session_is_refused(workspace):
    with pytest.raises(ValueError, match="No recording"):
        record.stop("plates_human")


def test_the_driver_and_eligibility_of_an_agent_run(workspace):
    def run(**context):
        base = {
            "episodes": [0, 1],
            "workflow": {"kind": "temporal"},
            "provider": "qwen-local",
            "supervision": "none",
        }
        return {
            "status": "succeeded",
            "dataset_key": "plates_agentVLM",
            "provider_config": {"kind": "ollama"},
            "context": {**base, **context},
        }

    assert record.driver(run()) == "local-vlm"
    assert record.driver(run(supervision="supervised")) == "local-vlm-teacher"
    assert record.eligible(run())
    assert not record.eligible(run(episodes=[0])), "only a full-dataset task"
    assert not record.eligible(run(workflow={"kind": "review"}))
    external = run()
    external["provider_config"] = {"kind": "external"}
    assert record.driver(external) == "external-mcp"
    api = run()
    api["provider_config"] = {"kind": "openai-compatible"}
    assert record.driver(api) == "api"


def test_the_editor_confirms_episodes_keeps_a_vocabulary_and_records(client, tmp_path):
    from test_formats import lerobot_fixture

    lerobot = lerobot_fixture(tmp_path / "lr")
    repo = client.post("/api/levi/catalog", json={"path": str(lerobot)}).json()["id"]
    api = "/annotations/api"
    body = {"repo_id": repo}

    vocab = client.get(f"{api}/dataset/vocabulary", params=body).json()
    assert vocab["subtasks"] == [] and "other" in vocab["special"]
    bad = client.post(
        f"{api}/dataset/vocabulary", json={**body, "subtasks": [{"id": "Grasp!"}]}
    )
    assert bad.status_code == 400
    saved = client.post(
        f"{api}/dataset/vocabulary",
        json={**body, "subtasks": [{"id": "grasp", "label": "grasp"}]},
    ).json()
    assert saved["subtasks"][0]["id"] == "grasp"

    assert client.get(f"{api}/eval/recording", params=body).json()["session"] is None
    stop = client.post(f"{api}/eval/recording/stop", json=body)
    assert stop.status_code == 409, "nothing to stop"
    started = client.post(f"{api}/eval/recording/start", json=body).json()["session"]
    assert started["started_at"] > 0

    atoms = [
        {
            "role": "assistant",
            "content": "close on the handle",
            "style": "subtask",
            "timestamp": 0.0,
            "levi": {"subtask_id": "grasp", "outcome": "success"},
        }
    ]
    client.post(
        f"{api}/episodes/0/atoms",
        json={**body, "episode_index": 0, "atoms": atoms},
    )
    assert client.post(f"{api}/episodes/99/status", json=body).status_code == 404
    confirmed = client.post(f"{api}/episodes/0/status", json={**body, "done": True})
    assert confirmed.json()["status"]["done"] is True
    status = client.get(f"{api}/episodes/status", params=body).json()["status"]
    assert list(status) == ["0"]

    result = client.post(f"{api}/eval/recording/stop", json=body).json()
    assert result["episodes"] == 1 and result["segments"] == 1
    assert result["coverage_seconds"] == pytest.approx(1.0)
    text = Path(result["path"]).read_text()
    assert "带词表子任务 id 的比例 | 100.0%" in text
    assert client.get(f"{api}/eval/recording", params=body).json()["session"] is None
