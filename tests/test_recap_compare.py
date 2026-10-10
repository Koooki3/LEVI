"""Historical browsing and paired comparisons on published sidecars."""

import numpy as np
import pytest

from levi.recap import advantage, compare, jobs, store


@pytest.fixture(autouse=True)
def _original_layout(monkeypatch):
    """These tests describe the original one-revision-per-run layout
    (``LEVI_RECAP_STORE_LAYOUT=revisions``, main's behaviour before the
    per-model default); tests/test_recap_store.py covers the default."""
    monkeypatch.setenv("LEVI_RECAP_STORE_LAYOUT", "revisions")


@pytest.fixture
def revisions(client, monkeypatch, tmp_path):
    ds = jobs.Dataset(
        "local/shared",
        "shared",
        tmp_path,
        {"fps": 10},
        {
            0: {"length": 8, "levi_outcome": "failure"},
            1: {"length": 8, "levi_outcome": "failure"},
            2: {"length": 8, "levi_outcome": "success"},
        },
        {},
    )
    monkeypatch.setattr(jobs, "dataset", lambda _repo: ds)
    monkeypatch.setattr(ds, "fingerprint", lambda: {"dataset_revision": "same-data"})

    def publish(checkpoint_name, episodes, **changes):
        meta = {
            "checkpoint": {
                "name": checkpoint_name,
                "manifest": {
                    "step": 1,
                    "v_min": -1.0,
                    "v_max": 0.0,
                    "precision": "bfloat16",
                },
            },
            "provider": "fake",
            "threshold": 0.01,
            "threshold_source": "checkpoint",
            "positive_quantile": 0.3,
            "lookahead": 10,
            "gamma": 1.0,
            "failure_reward": -300.0,
            "dataset_type": "rollout",
            "return_min": -488.0,
            "return_max": 0.0,
            "fingerprint": {"dataset_revision": "same-data"},
            "outcomes": {"0": "success", "1": "failure", "2": "success"},
            "fps": 10,
            **changes,
        }
        arrays = {}
        for ep, frames, values, positive in episodes:
            n = len(frames)
            arrays[ep] = {
                "frame_index": np.array(frames),
                "timestamp": np.array(frames) / 10,
                "value": np.array(values),
                "value_next": np.zeros(n),
                "advantage": np.where(positive, 0.03, -0.01),
                "positive": np.array(positive),
                "return": -np.arange(n, 0, -1),
                "reward_sum": -np.ones(n),
                "reward_sum_raw": -np.ones(n),
                "num_valid_rewards": np.ones(n),
            }
        return store.publish(ds.name, arrays, meta, dataset_name=ds.name)["revision_id"]

    return client, ds, publish


def test_historical_reads_are_explicit_and_leave_current_unchanged(revisions):
    client, ds, publish = revisions
    a = publish("r1", [(0, [0, 2], [-0.5, -0.4], [False, True])])
    b = publish(
        "r2",
        [(0, [0, 2], [-0.3, -0.2], [True, True])],
        threshold=0.005278945887678077,
        return_min=-799.0,
    )
    before = (store.root(ds.name) / "current.json").read_bytes()
    response = client.get(
        "/annotations/api/recap/revisions", params={"repo_id": ds.repo_id}
    )
    assert response.status_code == 200
    assert [r["revision_id"] for r in response.json()["revisions"]] == [b, a]
    assert response.json()["current"] == b
    assert response.json()["revisions"][1]["checkpoint"] == "r1"
    for route in ("summary", "episodes/0"):
        response = client.get(
            f"/annotations/api/recap/{route}",
            params={"repo_id": ds.repo_id, "revision_id": a},
        )
        assert response.status_code == 200
        assert response.json()["revision_id"] == a
    assert (store.root(ds.name) / "current.json").read_bytes() == before
    assert jobs.episode_payload(ds.repo_id, 0)["revision_id"] == b


def test_comparison_joins_frames_and_counts_exclusive_episodes(revisions):
    client, ds, publish = revisions
    a = publish(
        "r1",
        [
            (0, [0, 2, 4], [-0.5, -0.4, -0.3], [False, True, True]),
            (1, [0, 1], [-0.7, -0.7], [False, False]),
        ],
    )
    b = publish(
        "r2",
        [
            (0, [1, 2, 4], [-0.8, -0.2, -0.1], [False, False, True]),
            (2, [0], [-0.1], [True]),
        ],
        return_min=-799.0,
        threshold=0.005278945887678077,
    )
    before = (store.root(ds.name) / "current.json").read_bytes()
    response = client.get(
        "/annotations/api/recap/compare", params={"repo_id": ds.repo_id, "a": a, "b": b}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["episodes"] == {"shared": 1, "only_a": 1, "only_b": 1}
    assert result["frames"] == {"shared": 2, "only_a": 3, "only_b": 2}
    assert result["labels"]["agreement"] == 0.5
    assert result["labels"]["positive_a_only"] == 1
    assert result["labels"]["positive_b_only"] == 0
    assert result["value"]["mean_abs_diff"] == pytest.approx(0.2)
    assert result["value_return_units"]["mean_a"] == pytest.approx(-0.35 * 488)
    assert result["value_return_units"]["mean_b"] == pytest.approx(-0.15 * 799)
    assert {"return_range_differs", "threshold_differs", "coverage_differs"} <= set(
        result["notes"]
    )
    assert (store.root(ds.name) / "current.json").read_bytes() == before


def test_saved_outcomes_are_used_after_current_labels_change(revisions):
    _, ds, publish = revisions
    episodes = [
        (0, [0, 1], [-0.2, -0.2], [True, True]),
        (1, [0, 1], [-0.8, -0.8], [False, False]),
    ]
    a, b = publish("r1", episodes), publish("r2", episodes)
    result = compare.compare_payload(ds.repo_id, a, b)
    assert result["a"]["stale"] is True
    assert result["outcome_separation"]["auc_a"] == 1.0
    assert result["outcome_separation"]["success_episodes"] == 1
    assert "stale_results" in result["notes"]
    c = publish("r3", episodes, outcomes={"0": "failure", "1": "failure"})
    result = compare.compare_payload(ds.repo_id, a, c)
    assert result["outcome_separation"] is None
    assert "outcomes_differ" in result["notes"]
    assert result["per_episode"][0]["outcome"] is None


def test_disjoint_frames_and_constant_curves_have_null_metrics(revisions):
    _, ds, publish = revisions
    a = publish("r1", [(0, [0], [-0.5], [True])])
    b = publish("r2", [(0, [1], [-0.5], [True])])
    result = compare.compare_payload(ds.repo_id, a, b)
    assert result["frames"] == {"shared": 0, "only_a": 1, "only_b": 1}
    assert result["episodes"] == {"shared": 1, "only_a": 0, "only_b": 0}
    assert result["labels"] is result["value"] is result["advantage"] is None
    c = publish("r3", [(0, [0], [-0.5], [True])])
    assert compare.compare_payload(ds.repo_id, a, c)["value"]["corr"] is None


def test_metadata_only_fingerprints_do_not_claim_source_verification(revisions):
    _, ds, publish = revisions
    episodes = [(0, [0, 1], [-0.5, -0.4], [False, True])]
    a, b = publish("r1", episodes), publish("r2", episodes)
    assert (
        "fingerprint_unavailable" in compare.compare_payload(ds.repo_id, a, b)["notes"]
    )
    strong = {"source_fingerprint": "same-payload", "dataset_revision": "same-data"}
    c = publish("r3", episodes, fingerprint=strong)
    d = publish("r4", episodes, fingerprint=strong)
    assert (
        "fingerprint_unavailable"
        not in compare.compare_payload(ds.repo_id, c, d)["notes"]
    )
    assert (
        "fingerprint_unavailable" in compare.compare_payload(ds.repo_id, a, c)["notes"]
    )


def test_bin_count_difference_is_disclosed(revisions):
    _, ds, publish = revisions
    episodes = [(0, [0, 1], [-0.5, -0.4], [False, True])]
    a = publish(
        "r1", episodes, checkpoint={"name": "r1", "manifest": {"num_bins": 201}}
    )
    b = publish(
        "r2", episodes, checkpoint={"name": "r2", "manifest": {"num_bins": 101}}
    )
    result = compare.compare_payload(ds.repo_id, a, b)
    assert result["a"]["value_support"]["num_bins"] == 201
    assert "value_support_differs" in result["notes"]


def test_auc_ties_and_large_groups_use_linear_memory():
    assert compare._auc(
        np.array([1.0, 2.0]), np.array([0.0, 1.0, 2.0])
    ) == pytest.approx(2 / 3)
    assert compare._auc(np.zeros(20_000), np.zeros(20_000)) == 0.5
    assert compare._auc(np.array([]), np.array([0.0])) is None


@pytest.mark.parametrize("route", ["summary", "episodes/0"])
def test_revision_selection_rejects_invalid_and_unpublished_ids(revisions, route):
    client, ds, publish = revisions
    publish("r1", [(0, [0], [-0.5], [True])])
    for rid, expected in [("../../outside", 400), ("", 400), ("20200101-0000", 404)]:
        response = client.get(
            f"/annotations/api/recap/{route}",
            params={"repo_id": ds.repo_id, "revision_id": rid},
        )
        assert response.status_code == expected, response.text


def test_changed_source_and_missing_episode_refuse_comparison(revisions):
    _, ds, publish = revisions
    episodes = [(0, [0, 1], [-0.5, -0.4], [False, True])]
    a, b = (
        publish("r1", episodes),
        publish("r2", episodes, fingerprint={"dataset_revision": "new-data"}),
    )
    with pytest.raises(jobs.RecapError, match="dataset changed") as exc:
        compare.compare_payload(ds.repo_id, a, b)
    assert exc.value.status == 409
    c = publish("r3", episodes)
    (store.root(ds.name) / "revisions" / c / "episode-000000.parquet").unlink()
    with pytest.raises(jobs.RecapError, match="missing episode"):
        compare.compare_payload(ds.repo_id, a, c)


def test_duplicate_ids_and_different_timestamps_are_refused(revisions):
    import pyarrow as pa
    import pyarrow.parquet as pq

    _, ds, publish = revisions
    a = publish("r1", [(0, [0, 1], [-0.5, -0.4], [False, True])])
    b = publish("r2", [(0, [0, 0], [-0.5, -0.4], [False, True])])
    with pytest.raises(jobs.RecapError, match="duplicate frame"):
        compare.compare_payload(ds.repo_id, a, b)
    c = publish("r3", [(0, [0, 1], [-0.5, -0.4], [False, True])])
    path = store.root(ds.name) / "revisions" / c / "episode-000000.parquet"
    table = pq.read_table(path)
    table = table.set_column(
        table.schema.get_field_index("timestamp"), "timestamp", pa.array([0.0, 0.3])
    )
    store.write_table(path, table)
    with pytest.raises(jobs.RecapError, match="timestamps differ"):
        compare.compare_payload(ds.repo_id, a, c)


def test_exact_r2_threshold_is_inclusive_and_sft_is_all_positive():
    threshold = 0.005278945887678077
    scores = np.array(
        [np.nextafter(threshold, -np.inf), threshold, np.nextafter(threshold, np.inf)]
    )
    assert advantage.label(scores, threshold).tolist() == [False, True, True]
    assert advantage.label(scores, threshold, sft=True).all()


def test_comparison_groups_by_outcome_and_bins_values(revisions):
    """The viewer's charts read these aggregates instead of every frame."""
    client, ds, publish = revisions
    a = publish(
        "r1",
        [
            (0, [0, 1], [-0.5, -0.3], [False, True]),
            (1, [0, 1], [-0.9, -0.7], [False, False]),
            (2, [0, 1], [-0.2, -0.1], [True, True]),
        ],
    )
    b = publish(
        "r2",
        [
            (0, [0, 1], [-0.4, -0.2], [True, True]),
            (1, [0, 1], [-0.8, -0.8], [False, True]),
            (2, [0, 1], [-0.2, -0.2], [True, True]),
        ],
        outcomes={"0": "success", "1": "failure", "2": "failure"},
    )
    response = client.get(
        "/annotations/api/recap/compare", params={"repo_id": ds.repo_id, "a": a, "b": b}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    groups = {g["outcome"]: g for g in result["by_outcome"]}
    # Episode 2's saved outcome differs between the runs: it is "unknown".
    assert set(groups) == {"success", "failure", "unknown"}
    assert groups["success"]["episodes"] == 1
    assert groups["success"]["mean_value_a"] == pytest.approx(-0.4)
    assert groups["success"]["mean_value_b"] == pytest.approx(-0.3)
    assert groups["success"]["label_agreement"] == pytest.approx(0.5)
    assert groups["failure"]["positive_fraction_b"] == pytest.approx(0.5)
    assert groups["unknown"]["frames"] == 2
    dist = result["distribution"]
    assert dist["bins"] == compare.HISTOGRAM_BINS
    value = dist["value"]
    assert len(value["edges"]) == compare.HISTOGRAM_BINS + 1
    assert sum(value["a"]) == sum(value["b"]) == result["frames"]["shared"] == 6
    assert value["edges"][0] == pytest.approx(-0.9)
    assert value["edges"][-1] == pytest.approx(-0.1)
    diff = dist["abs_diff"]
    assert sum(diff["counts"]) == 6
    assert diff["edges"][0] == 0.0
    assert diff["edges"][-1] == pytest.approx(0.1)


def test_constant_values_still_get_bins_and_no_overlap_gives_empty_aggregates(
    revisions,
):
    _, ds, publish = revisions
    a = publish("r1", [(0, [0, 1], [-0.5, -0.5], [True, True])])
    b = publish("r2", [(0, [0, 1], [-0.5, -0.5], [True, True])])
    result = compare.compare_payload(ds.repo_id, a, b)
    value = result["distribution"]["value"]
    assert value["edges"][-1] > value["edges"][0]
    assert sum(value["a"]) == 2
    assert sum(result["distribution"]["abs_diff"]["counts"]) == 2
    c = publish("r3", [(1, [0], [-0.2], [True])])
    empty = compare.compare_payload(ds.repo_id, a, c)
    assert empty["by_outcome"] == [] and empty["distribution"] is None


def test_value_only_side_keeps_value_aggregates_without_label_rates(revisions):
    _, ds, publish = revisions
    a = publish("r1", [(0, [0, 1], [-0.5, -0.4], [True, False])])
    b = publish(
        "r2",
        [(0, [0, 1], [-0.3, -0.2], [True, True])],
        dataset_type="value_only",
        labels=False,
        threshold=None,
        threshold_source=None,
        return_min=None,
        return_max=None,
        outcomes={},
    )
    result = compare.compare_payload(ds.repo_id, a, b)
    assert result["labels"] is None
    (group,) = result["by_outcome"]
    assert group["outcome"] == "unknown"
    assert group["label_agreement"] is None
    assert group["positive_fraction_a"] is None
    assert sum(result["distribution"]["value"]["b"]) == 2
