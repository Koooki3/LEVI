"""RECAP results keyed by (dataset, value model): atomic head switch, subset
merge, grace-period reclamation, crash injection, reader pinning and the
original ``revisions/`` layout read side by side."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import pytest
from test_recap_value import BODY, run

from levi import catalog
from levi.recap import checkpoints, compare, jobs, store

PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture
def models_layout(monkeypatch):
    monkeypatch.setenv("LEVI_RECAP_STORE_LAYOUT", "models")


def _episode(n, offset=0.0):
    frames = np.arange(n)
    adv = np.where(frames % 2 == 0, 0.03, -0.01)
    return {
        "frame_index": frames,
        "timestamp": frames / 10,
        "value": -0.5 + offset + frames / 100,
        "value_next": np.zeros(n),
        "advantage": adv,
        "positive": adv >= 0.0,
        "return": -np.arange(n, 0, -1),
        "reward_sum": -np.ones(n),
        "reward_sum_raw": -np.ones(n),
        "num_valid_rewards": np.ones(n, dtype=np.int64),
    }


def _meta(model="r1", **changes):
    return {
        "checkpoint": {
            "name": model,
            "sha256": "a" * 64,
            "manifest": {"num_bins": 201, "v_min": -1.0, "v_max": 0.0},
        },
        "provider": "fake",
        "threshold": 0.0,
        "threshold_source": "checkpoint",
        "positive_quantile": 0.3,
        "lookahead": 10,
        "gamma": 1.0,
        "failure_reward": -300.0,
        "dataset_type": "rollout",
        "return_min": -10.0,
        "return_max": 0.0,
        "fingerprint": {"dataset_revision": "same"},
        "outcomes": {"0": "success", "1": "failure", "2": "success"},
        "static_filter": {"mode": "auto", "applied": False},
        "fps": 10,
        **changes,
    }


def _publish(episodes, model="r1", subset=False, **changes):
    return store.publish_model(
        "ds",
        {ep: _episode(n, off) for ep, (n, off) in episodes.items()},
        _meta(model, **changes),
        dataset_name="ds",
        subset=subset,
    )


def _values(model, ep, version=None):
    table = store.read_episode("ds", ep, model, version)
    return None if table is None else table.column("value").to_pylist()


def _versions(model="r1"):
    return sorted(p.name for p in (store.root("ds") / "models" / model / "v").iterdir())


# ---------------------------------------------------------------- store


def test_recompute_replaces_and_subset_merges(client):
    first = _publish({0: (8, 0.0), 1: (6, 0.0), 2: (4, 0.0)})
    assert first["revision_id"] == "r1" and first["layout"] == "models"
    assert store.current_ref("ds") == "r1" and store.models("ds") == ["r1"]
    old_ep1 = (
        store.root("ds") / "models/r1/v" / first["version"] / "episode-000001.parquet"
    )
    # A subset with the same parameters merges: episode 0 is new, the others
    # are hard links to the previous version's files.
    merged = _publish({0: (8, 0.1)}, subset=True)
    assert merged["merged"] and merged["previous_version"] == first["version"]
    assert merged["episode_indices"] == [0, 1, 2] and merged["frames"] == 18
    new_dir = store.root("ds") / "models/r1/v" / merged["version"]
    assert os.stat(new_dir / "episode-000001.parquet").st_ino == old_ep1.stat().st_ino
    assert _values("r1", 0)[0] == pytest.approx(-0.4)
    assert _values("r1", 1)[0] == pytest.approx(-0.5)
    summary = store.summary("ds", "r1")
    assert set(summary["episodes"]) == {"0", "1", "2"}
    assert (
        summary["episodes"]["0"]["computed_at"]
        > summary["episodes"]["1"]["computed_at"]
    )
    assert summary["episodes"]["0"]["min_value"] == pytest.approx(-0.4)
    import pyarrow.parquet as pq

    table = pq.read_table(new_dir / "advantages.parquet")
    assert table.num_rows == 18
    assert table.column("episode_index").to_pylist() == [0] * 8 + [1] * 6 + [2] * 4
    assert merged["outcomes"] == {"0": "success", "1": "failure", "2": "success"}
    # A subset whose parameters differ is refused and changes nothing.
    with pytest.raises(store.MergeRefused, match="threshold"):
        _publish({1: (6, 0.2)}, subset=True, threshold=0.5)
    assert store.head("ds", "r1") == merged["version"]
    assert len(_versions()) == 2
    # A full recomputation replaces the whole result, whatever its parameters.
    full = _publish({1: (6, 0.3)}, threshold=0.5)
    assert full["episode_indices"] == [1] and not full["merged"]
    assert _values("r1", 0) is None
    # One row per model in the listing; the previous versions only wait for
    # their grace period.
    assert store.results("ds") == ["r1"]
    retired = [
        v
        for v in _versions()
        if (store.root("ds") / "models/r1/v" / v / "RETIRED").is_file()
    ]
    assert len(retired) == 2 and full["version"] not in retired


def test_two_models_keep_two_results_and_current_follows_the_last(client):
    a = _publish({0: (8, 0.0)}, model="r1")
    b = _publish({0: (8, 0.1)}, model="r2")
    assert store.results("ds") == ["r1", "r2"]
    assert store.current_ref("ds") == "r2"
    assert store.revision("ds")["version"] == b["version"]
    assert store.revision("ds", "r1")["version"] == a["version"]


def _crash_on(monkeypatch, filename):
    real = store._durable_json

    def crashing(path, value):
        if Path(path).name == filename:
            raise KeyboardInterrupt(f"killed before {filename}")
        real(path, value)

    monkeypatch.setattr(store, "_durable_json", crashing)
    return real


def test_crash_before_the_head_switch_leaves_the_old_version_live(client, monkeypatch):
    first = _publish({0: (8, 0.0), 1: (6, 0.0)})
    # Killed just before the switch: a complete new version, never live.
    real = _crash_on(monkeypatch, "head.json")
    with pytest.raises(KeyboardInterrupt):
        _publish({0: (8, 0.2), 1: (6, 0.2)})
    assert store.head("ds", "r1") == first["version"]
    assert store.revision("ds")["version"] == first["version"]
    assert _values("r1", 1)[0] == pytest.approx(-0.5)
    # Killed while writing the next version: an unfinished directory. (That
    # publication's own sweep found the complete orphan and started its
    # grace period instead of trusting it.)
    _crash_on(monkeypatch, "result.json")
    with pytest.raises(KeyboardInterrupt):
        _publish({0: (8, 0.1), 1: (6, 0.1)})
    assert store.head("ds", "r1") == first["version"]
    assert _values("r1", 0)[0] == pytest.approx(-0.5)
    assert len(_versions()) == 3
    monkeypatch.setattr(store, "_durable_json", real)
    # The next sweep removes the unfinished one at once ...
    done = store.sweep("ds")
    assert len(done["removed"]) == 1 and done["retired"] == []
    assert len(_versions()) == 2
    # ... and the orphan after its grace period.
    later = store.sweep("ds", now=10**12)
    assert len(later["removed"]) == 1 and _versions() == [first["version"]]
    assert _values("r1", 0)[0] == pytest.approx(-0.5)


def test_crash_after_the_head_switch_keeps_the_old_version_readable(
    client, monkeypatch
):
    first = _publish({0: (8, 0.0)})
    real = _crash_on(monkeypatch, "current.json")
    with pytest.raises(KeyboardInterrupt):
        _publish({0: (8, 0.1)})
    monkeypatch.setattr(store, "_durable_json", real)
    new = store.head("ds", "r1")
    assert new != first["version"]
    assert _values("r1", 0)[0] == pytest.approx(-0.4)
    # A reader that resolved the old version reads it on: never marked, so
    # the sweep only starts its grace period.
    assert _values("r1", 0, first["version"])[0] == pytest.approx(-0.5)
    assert store.sweep("ds")["retired"] == [f"r1/{first['version']}"]
    assert _values("r1", 0, first["version"])[0] == pytest.approx(-0.5)


def test_a_killed_process_leaves_the_old_version_and_releases_the_lock(client):
    """A real kill (os._exit) at the head switch, in a child process."""
    first = _publish({0: (8, 0.0)})
    script = f"""
import os, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, {str(PROJECT / "tests")!r})
from levi import catalog
catalog.STATE = Path({str(catalog.STATE)!r})
from levi.recap import store
import test_recap_store as t
real = os.replace
def replace(src, dst, *a, **k):
    if str(dst).endswith("head.json"):
        os._exit(9)
    return real(src, dst, *a, **k)
os.replace = replace
t._publish({{0: (8, 0.3)}})
"""
    done = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": str(PROJECT)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 9, done.stderr
    assert store.head("ds", "r1") == first["version"]
    assert _values("r1", 0)[0] == pytest.approx(-0.5)
    # The flock died with the process: the next publication goes through.
    again = _publish({0: (8, 0.1)})
    assert store.head("ds", "r1") == again["version"]


def test_readers_never_see_a_missing_file_while_results_are_replaced(client):
    _publish({0: (40, 0.0), 1: (40, 0.0)})
    stop, failures, reads = threading.Event(), [], [0]

    def reader():
        while not stop.is_set():
            record = store.revision("ds", "r1")
            for ep in (0, 1):
                table = store.read_episode("ds", ep, "r1", record["version"])
                if table is None or table.num_rows != 40:
                    failures.append((record["version"], ep))
            reads[0] += 1

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for thread in threads:
        thread.start()
    try:
        for i in range(6):
            _publish({0: (40, i / 100)}, subset=True)
            _publish({0: (40, i / 100), 1: (40, i / 100)})
    finally:
        stop.set()
        for thread in threads:
            thread.join(30)
    assert not failures and reads[0] > 0


def test_grace_period_then_reclaim_keeps_linked_files(client):
    first = _publish({0: (8, 0.0), 1: (6, 0.0)})
    second = _publish({1: (6, 0.1)}, subset=True)  # episode 0 is a hard link
    assert store.sweep("ds")["removed"] == []
    assert _values("r1", 0, first["version"]) is not None
    removed = store.sweep("ds", now=10**12)["removed"]
    assert removed == [f"r1/{first['version']}"]
    assert _values("r1", 0, first["version"]) is None
    assert _values("r1", 0)[0] == pytest.approx(-0.5)  # the link survives
    assert store.head("ds", "r1") == second["version"]


def test_original_layout_is_read_beside_model_results(client, monkeypatch):
    monkeypatch.delenv("LEVI_RECAP_STORE_LAYOUT", raising=False)
    meta = _meta("r1")
    old = store.publish(
        "ds", {0: _episode(8)}, meta, dataset_name="ds"
    )  # default: the original layout
    rid = old["revision_id"]
    assert (store.root("ds") / "revisions" / rid / "revision.json").is_file()
    assert (
        store.current_ref("ds") == rid and store.revision("ds")["layout"] == "revisions"
    )
    # A summary written before min/max existed is still served as it is.
    path = store.root("ds") / "revisions" / rid / "summary.json"
    summary = json.loads(path.read_text())
    for row in summary["episodes"].values():
        row.pop("min_value"), row.pop("max_value")
    path.write_text(json.dumps(summary))
    assert "min_value" not in store.summary("ds")["episodes"]["0"]
    monkeypatch.setenv("LEVI_RECAP_STORE_LAYOUT", "models")
    new = store.publish("ds", {0: _episode(8, 0.1)}, meta, dataset_name="ds")
    assert new["revision_id"] == "r1" and new["layout"] == "models"
    assert store.current_ref("ds") == "r1"
    assert store.results("ds") == ["r1", rid]
    # The old revision stays readable by its id and is never touched.
    assert store.revision("ds", rid)["version"] == rid
    assert _values(rid, 0)[0] == pytest.approx(-0.5)
    # A merge never mixes a model result with an original-layout revision.
    assert store.revision("ds", "r1")["merged"] is False
    monkeypatch.setenv("LEVI_RECAP_STORE_LAYOUT", "bogus")
    with pytest.raises(ValueError, match="LEVI_RECAP_STORE_LAYOUT"):
        store.publish("ds", {0: _episode(8)}, meta, dataset_name="ds")


def test_pool_signal_reads_the_current_model_result(client):
    from levi.pool import recap_signal

    _publish({0: (8, 0.0), 1: (6, 0.0)})
    summary = recap_signal._current_summary(store.root("ds"))
    assert set(summary["episodes"]) == {"0", "1"}
    assert summary["episodes"]["0"]["positive_fraction"] == 0.5
    (store.root("ds") / "current.json").write_text(json.dumps({"model": "../x"}))
    assert recap_signal._current_summary(store.root("ds")) is None


# ---------------------------------------------------------------- routes


@pytest.fixture
def recap_models(client, models_layout, monkeypatch, tmp_path):
    from test_formats import capture_fixture
    from test_views import register_raw

    monkeypatch.setenv("LEVI_RECAP_VALUE_CHECKPOINT_DIR", str(tmp_path / "ckpt"))
    monkeypatch.setenv("LEVI_RECAP_VALUE_WORKER_PYTHON", str(tmp_path / "no-python"))
    checkpoints.import_checkpoint(
        None, "fake-a", provider="fake", fields={"failure_reward": -30.0}
    )
    entry = register_raw(client, capture_fixture(tmp_path / "plates"))
    yield client, entry
    assert not jobs.wait_idle(60)


def test_same_model_twice_is_one_result_and_pins_answer_409(recap_models):
    client, entry = recap_models
    repo, name = entry["id"], entry["name"]
    first = run(client, repo)
    assert first["status"] == "succeeded" and first["revision_id"] == "fake-a"
    v1 = client.get(
        "/annotations/api/recap/episodes/0", params={"repo_id": repo}
    ).json()
    assert v1["revision_id"] == "fake-a" and v1["model"] == "fake-a"
    second = run(client, repo)
    assert second["revision_id"] == "fake-a"
    listing = client.get("/annotations/api/recap/results", params={"repo_id": repo})
    rows = listing.json()["results"]
    assert [r["revision_id"] for r in rows] == ["fake-a"]
    assert listing.json()["current"] == "fake-a" and rows[0]["layout"] == "models"
    assert rows[0]["version"] != v1["version"]
    assert not (store.root(name) / "revisions").exists()
    # The old route name answers the same.
    assert (
        client.get("/annotations/api/recap/revisions", params={"repo_id": repo}).json()[
            "revisions"
        ]
        == rows
    )
    # A reader pinned to the replaced version is told to reload.
    for route in ("episodes/0", "summary"):
        stale = client.get(
            f"/annotations/api/recap/{route}",
            params={"repo_id": repo, "revision_id": "fake-a", "version": v1["version"]},
        )
        assert stale.status_code == 409 and "recomputed" in stale.json()["detail"]
        fresh = client.get(
            f"/annotations/api/recap/{route}",
            params={"repo_id": repo, "version": rows[0]["version"]},
        )
        assert (
            fresh.status_code == 200 and fresh.json()["version"] == rows[0]["version"]
        )
    bad = client.get(
        "/annotations/api/recap/summary",
        params={"repo_id": repo, "version": "../x"},
    )
    assert bad.status_code == 400
    status = client.get("/annotations/api/recap/status", params={"repo_id": repo})
    current = status.json()["current"]
    assert current["revision_id"] == "fake-a" and current["layout"] == "models"


def test_subset_runs_merge_or_are_refused_before_the_worker(recap_models):
    client, entry = recap_models
    repo, name = entry["id"], entry["name"]
    run(client, repo)
    # Without a stored threshold and return range, a subset's numbers would
    # come from its own statistics: refused before anything starts.
    refused = client.post(
        "/annotations/api/recap/run", json={**BODY, "repo_id": repo, "episodes": [0]}
    )
    assert refused.status_code == 409
    assert "recompute the whole dataset" in refused.json()["detail"]
    assert "threshold" in refused.json()["detail"]
    checkpoints.update(
        "fake-a", {"unified_threshold": 0.01, "return_min": -64.0, "return_max": 0.0}
    )
    full = run(client, repo)
    before = store.revision(name, "fake-a")
    assert before["episode_indices"] == [0, 1] and full["status"] == "succeeded"
    merged = run(client, repo, episodes=[1])
    assert merged["status"] == "succeeded", merged
    after = store.revision(name, "fake-a")
    assert after["merged"] and after["episode_indices"] == [0, 1]
    assert after["frames"] == 64 and after["previous_version"] == before["version"]
    summary = client.get(
        "/annotations/api/recap/summary", params={"repo_id": repo}
    ).json()
    assert set(summary["episodes"]) == {"0", "1"}
    # A different parameter on a subset is refused; on the whole dataset it
    # replaces the result.
    other = client.post(
        "/annotations/api/recap/run",
        json={**BODY, "repo_id": repo, "episodes": [1], "lookahead": 5},
    )
    assert other.status_code == 409 and "lookahead" in other.json()["detail"]
    replaced = run(client, repo, lookahead=5)
    assert store.revision(name, "fake-a")["lookahead"] == 5
    assert replaced["revision_id"] == "fake-a"


def test_comparison_pins_both_versions(recap_models):
    client, entry = recap_models
    repo = entry["id"]
    checkpoints.import_checkpoint(
        None, "fake-b", provider="fake", fields={"failure_reward": -20.0}
    )
    run(client, repo)
    run(client, repo, checkpoint="fake-b")
    rows = client.get(
        "/annotations/api/recap/results", params={"repo_id": repo}
    ).json()["results"]
    assert [r["revision_id"] for r in rows] == ["fake-b", "fake-a"]
    versions = {r["revision_id"]: r["version"] for r in rows}
    result = compare.compare_payload(repo, "fake-a", "fake-b")
    assert result["a"]["version"] == versions["fake-a"]
    assert result["episodes"]["shared"] == 2
    run(client, repo)  # fake-a recomputed
    stale = client.get(
        "/annotations/api/recap/compare",
        params={
            "repo_id": repo,
            "a": "fake-a",
            "b": "fake-b",
            "version_a": versions["fake-a"],
        },
    )
    assert stale.status_code == 409
    fresh = client.get(
        "/annotations/api/recap/compare",
        params={"repo_id": repo, "a": "fake-a", "b": "fake-b"},
    )
    assert fresh.status_code == 200, fresh.text


# ---------------------------------------------------------------- clear


def test_clear_is_a_dry_run_until_apply_and_covers_both_layouts(
    client, monkeypatch, tmp_path, capsys
):
    from levi.recap.cli import main

    ckpt = tmp_path / "ckpt"
    monkeypatch.setenv("LEVI_RECAP_VALUE_CHECKPOINT_DIR", str(ckpt))
    checkpoints.import_checkpoint(None, "keep-me", provider="fake")
    monkeypatch.delenv("LEVI_RECAP_STORE_LAYOUT", raising=False)
    rid = store.publish("ds", {0: _episode(8)}, _meta(), dataset_name="ds")[
        "revision_id"
    ]
    _publish({0: (8, 0.1)})
    store.write_json(store.root("ds") / "dataset.json", {"dataset_type": "sft"})
    (store.root("ds") / "jobs").mkdir()
    (store.root("ds") / "jobs" / "20261010-0000.json").write_text("{}")
    assert main(["clear"]) == 1  # neither names nor --all
    assert "--all" in capsys.readouterr().err
    assert main(["clear", "ds"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["applied"] is False and dry["bytes"] > 0
    kinds = {(i["kind"], i["ref"]) for i in dry["datasets"][0]["items"]}
    assert kinds == {("model_result", "r1"), ("revision", rid), ("current", None)}
    assert store.results("ds") == ["r1", rid]  # nothing deleted
    # A live job refuses it.
    real_running = jobs._running
    monkeypatch.setattr(jobs, "_running", lambda name: ["20261010-0001"])
    with pytest.raises(jobs.RecapError) as busy:
        jobs.clear(["ds"], apply=True)
    assert busy.value.status == 409 and store.results("ds") == ["r1", rid]
    monkeypatch.setattr(jobs, "_running", real_running)
    assert main(["clear", "ds", "--apply"]) == 0
    capsys.readouterr()
    folder = store.root("ds")
    assert store.results("ds") == [] and store.current_ref("ds") is None
    assert not (folder / "models").exists() and not (folder / "revisions").exists()
    assert (folder / "dataset.json").is_file()  # the setting stays
    assert (folder / "jobs").is_dir()  # only with --include-jobs
    log = [
        json.loads(line) for line in (folder / "cleared.jsonl").read_text().splitlines()
    ]
    assert {i["ref"] for i in log[0]["items"]} == {"r1", rid, None}
    assert (ckpt / "keep-me" / "manifest.json").is_file()  # checkpoints untouched
    report = jobs.clear(["ds"], include_jobs=True, apply=True)
    assert [i["kind"] for i in report["datasets"][0]["items"]] == ["jobs"]
    assert not (folder / "jobs").exists()
    with pytest.raises(jobs.RecapError) as missing:
        jobs.clear(["no-such-folder"])
    assert missing.value.status == 404
    with pytest.raises(jobs.RecapError):
        jobs.clear(["../outside"])
