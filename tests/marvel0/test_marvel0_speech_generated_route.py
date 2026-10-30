"""The speech answers carry a sentence generated from the datasets' most
common phrasing (owner decision 2026-10-30), read from the catalog."""

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from test_marvel0_speech_route import BASE, FakeManager, build_machine

from levi.marvel0 import models, speech_api
from levi.marvel0 import site as marvel0_site


def write_v2(root, tasks):
    (root / "meta").mkdir(parents=True)
    with (root / "meta/episodes.jsonl").open("w") as handle:
        for i, task in enumerate(tasks):
            handle.write(json.dumps({"episode_index": i, "tasks": [task]}) + "\n")


def write_v3(root, tasks):
    folder = root / "meta/episodes/chunk-000"
    folder.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {"episode_index": list(range(len(tasks))), "tasks": [[t] for t in tasks]}
        ),
        folder / "file-000.parquet",
    )


@pytest.fixture
def catalog_of(tmp_path, monkeypatch):
    from levi import catalog

    entries: dict[str, dict] = {}

    def make(name, tasks, *, v3=False, revision="r1", base=None):
        root = tmp_path / "data" / name
        (write_v3 if v3 else write_v2)(root, tasks)
        entries[name] = {"path": str(root), "revision": revision}
        if base:
            entries[name]["base"] = base
        return entries[name]

    monkeypatch.setattr(
        catalog, "datasets", lambda: {k: dict(v) for k, v in entries.items()}
    )
    monkeypatch.setattr(speech_api, "_frames_cache", None)
    return make


@pytest.fixture
def machine(tmp_path, monkeypatch):
    # The real matcher: the generated sentence comes from the real reader.
    yield build_machine(tmp_path, monkeypatch, FakeManager(), None)
    marvel0_site.clear_cache()
    models.cache_clear()


def test_instructions_are_counted_per_episode_in_v2_and_v3(catalog_of):
    catalog_of(
        "a", ["pick peach on green plate"] * 3 + ["Reset: put eggplant into blue bowl"]
    )
    catalog_of("b", ["pick peach on green plate", "pour water into red cup"], v3=True)
    catalog_of("ns", ["pick peach on green plate"] * 50, base="a")  # not counted twice
    key, counts = speech_api._dataset_instructions()
    assert [name for name, _ in key] == ["a", "b"]
    assert counts == {
        "pick peach on green plate": 4,
        "Reset: put eggplant into blue bowl": 1,
        "pour water into red cup": 1,
    }


def test_an_unreadable_dataset_gives_no_instructions(catalog_of, tmp_path):
    catalog_of("a", ["pick peach on green plate"])
    (tmp_path / "data/a/meta/episodes.jsonl").write_text("{not json\n")
    assert speech_api._dataset_instructions()[1] == {}


def test_frames_are_read_again_when_a_dataset_changes(catalog_of):
    catalog_of("a", ["pick peach on green plate"])
    first = speech_api._frames()
    assert speech_api._frames() is first  # cached
    catalog_of("b", ["put eggplant into blue bowl"] * 5, revision="r2")
    again = speech_api._frames()
    assert again is not first and again.by_action["A:MOVE"].verb == "put"


def test_a_spoken_chinese_task_comes_back_as_a_generated_sentence(machine, catalog_of):
    catalog_of(
        "a", ["pick peach on green plate"] * 5 + ["put eggplant into blue bowl"] * 2
    )
    response = machine.client.post(
        f"{BASE}/match", json={"transcript": "把茄子放到蓝色碗里"}
    )
    assert response.status_code == 200
    generated = response.json()["match"]["generated"]
    assert generated["status"] == "ok"
    assert generated["prompt"] == "pick eggplant into blue bowl"
    assert generated["dataset_original"] == "put eggplant into blue bowl"


def test_a_restricted_model_gets_no_generated_sentence(machine, catalog_of):
    catalog_of("a", ["pick peach on green plate"])
    response = machine.client.post(
        f"{BASE}/match",
        json={"transcript": "把茄子放到蓝色盘子里", "model_id": "pi05_fr3_sp_v1"},
    )
    generated = response.json()["match"]["generated"]
    assert (generated["status"], generated["prompt"]) == ("restricted", None)


def test_a_failing_generation_leaves_the_match_standing(
    machine, catalog_of, monkeypatch
):
    catalog_of("a", ["pick peach on green plate"])

    def broken():
        raise RuntimeError("catalog unreadable")

    monkeypatch.setattr(speech_api, "_frames", broken)
    response = machine.client.post(
        f"{BASE}/match", json={"transcript": "stack the plates"}
    )
    assert response.status_code == 200
    answer = response.json()["match"]
    assert answer["tier"] in {"A", "B", "C"}
    assert answer["generated"]["prompt"] is None


def test_malformed_rows_are_skipped_and_the_rest_still_counts(
    catalog_of, tmp_path, monkeypatch
):
    catalog_of("a", ["pick peach on green plate"] * 2)
    with (tmp_path / "data/a/meta/episodes.jsonl").open("a") as handle:
        handle.write('[1]\n"x"\n{not json\n' + json.dumps({"tasks": "a string"}) + "\n")
    folder = tmp_path / "data/b/meta/episodes/chunk-000"
    folder.mkdir(parents=True)
    pq.write_table(
        pa.table({"tasks": ["pour water into red cup", None]}),
        folder / "file-000.parquet",
    )
    from levi import catalog

    entries = catalog.datasets()
    entries["b"] = {"path": str(tmp_path / "data/b"), "revision": "r1"}

    monkeypatch.setattr(catalog, "datasets", lambda: entries)
    key, counts = speech_api._dataset_instructions()
    frames = speech_api._frames()
    assert counts == {"pick peach on green plate": 2}
    assert [name for name, _ in key] == ["a", "b"]
    assert frames.by_action["A:MOVE"].verb == "pick"


def test_an_entry_without_a_folder_does_not_defeat_the_cache(catalog_of, monkeypatch):
    from levi import catalog

    catalog_of("a", ["pick peach on green plate"])
    real = catalog.datasets
    monkeypatch.setattr(
        catalog, "datasets", lambda: {**real(), "ghost": {"revision": "r9"}}
    )
    first = speech_api._frames()
    assert speech_api._frames() is first
