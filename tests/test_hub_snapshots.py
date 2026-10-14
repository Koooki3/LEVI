"""Hub snapshots for diagnostics live in a size-limited DiskBudget (levi/hub_snapshots.py)."""

import pytest

from levi import hub_snapshots
from levi.hub_snapshots import reserve_for, snapshot
from levi.performance.budget import BudgetRejected

KIB = 1024
GIB = 1024**3


def downloader(calls, size=10 * KIB):
    def download(folder):
        calls.append(str(folder))
        (folder / "meta").mkdir(parents=True, exist_ok=True)
        (folder / "meta" / "info.json").write_bytes(b"x" * size)

    return download


def take(name, calls, tmp_path, size=10 * KIB, **kwargs):
    kwargs.setdefault("revision", "r1")
    kwargs.setdefault("sizes", [size])
    return snapshot(name, ["meta/**"], downloader(calls, size), tmp_path, **kwargs)


def test_a_snapshot_is_downloaded_once_and_then_reused(tmp_path):
    calls = []
    for _ in range(2):
        with take("org/ds", calls, tmp_path) as (folder, note):
            assert (folder / "meta" / "info.json").is_file() and note == ""
    assert len(calls) == 1


def test_different_patterns_are_different_snapshots(tmp_path):
    calls = []
    for patterns in (["meta/**"], ["meta/**", "videos/**"]):
        with snapshot(
            "org/ds", patterns, downloader(calls), tmp_path, revision="r1", sizes=[1]
        ):
            pass
    assert len(calls) == 2


def test_a_new_hub_commit_is_downloaded_again(tmp_path):
    calls = []
    for revision in ("r1", "r1", "r2"):
        with take("org/ds", calls, tmp_path, revision=revision):
            pass
    assert len(calls) == 2


def test_another_credential_never_reads_a_cached_snapshot(tmp_path):
    calls = []
    for scope in ("account-a", "account-b", "anonymous"):
        with take("org/private", calls, tmp_path, scope=scope):
            pass
    assert len(calls) == 3


def test_the_least_recently_used_snapshot_is_evicted_to_stay_under_the_limit(tmp_path):
    cap = 200 * KIB / GIB
    calls = []
    for name in ("org/a", "org/b", "org/c", "org/d", "org/e", "org/a"):
        with take(name, calls, tmp_path, size=40 * KIB, cap_gb=cap):
            pass
    # five snapshots with their reservation do not all fit in 200 KiB: "a" was evicted and fetched again
    assert len(calls) == 6
    total = sum(
        p.stat().st_size for p in (tmp_path / "hub-snapshots").rglob("*") if p.is_file()
    )
    assert total <= 200 * KIB + 8 * KIB


def test_a_snapshot_too_large_for_the_cache_is_downloaded_once_into_the_old_folder(
    tmp_path,
):
    calls = []
    cap = 20 * KIB / GIB
    with take("org/big", calls, tmp_path, size=40 * KIB, cap_gb=cap) as (folder, note):
        assert folder == tmp_path / "org__big"
        assert (folder / "meta" / "info.json").stat().st_size == 40 * KIB
        assert note.startswith("not cached (too_large)")
    # known from the sizes before downloading: no attempt inside the cache first
    assert calls == [str(tmp_path / "org__big")]
    assert not any((tmp_path / "hub-snapshots").rglob("info.json"))


@pytest.mark.parametrize("reason", ["unavailable", "lock_timeout"])
def test_a_cache_that_cannot_be_used_falls_back_instead_of_failing(
    tmp_path, monkeypatch, reason
):
    def refuse(*args, **kwargs):
        raise BudgetRejected(reason, "test")

    monkeypatch.setattr(hub_snapshots.DiskBudget, "from_cap_gb", refuse)
    calls = []
    with take("org/ds", calls, tmp_path) as (folder, note):
        assert folder == tmp_path / "org__ds"
        assert note.startswith(f"not cached ({reason})")
    assert len(calls) == 1


def test_a_snapshot_being_inspected_is_never_evicted(tmp_path):
    cap = 100 * KIB / GIB
    calls = []
    with take("org/held", calls, tmp_path, cap_gb=cap) as (held, _):
        for name in ("org/b", "org/c", "org/d"):
            with take(name, calls, tmp_path, cap_gb=cap):
                pass
        assert (held / "meta" / "info.json").stat().st_size == 10 * KIB


def test_the_reservation_covers_the_files_and_the_hub_bookkeeping():
    assert reserve_for([]) > 0
    assert reserve_for([10, 20]) > 30


def fake_hub(monkeypatch, dataset, calls, commits):
    import shutil
    from types import SimpleNamespace

    import huggingface_hub

    def dataset_info(self, repo_id, token=None, files_metadata=False):
        assert files_metadata
        names = ["meta/info.json", "data/chunk-000/file-000.parquet", "videos/a.mp4"]
        siblings = [SimpleNamespace(rfilename=n, size=1000) for n in names]
        return SimpleNamespace(sha=commits[0], siblings=siblings)

    def snapshot_download(
        repo_id, token, repo_type, revision, local_dir, allow_patterns
    ):
        calls.append((repo_id, revision, tuple(allow_patterns)))
        shutil.copytree(dataset, local_dir, dirs_exist_ok=True)

    monkeypatch.setattr(huggingface_hub.HfApi, "dataset_info", dataset_info)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)


def test_a_hub_diagnostic_downloads_once_per_commit_into_the_budget(
    client, dataset, tmp_path, monkeypatch
):
    from levi import service

    calls, commits = [], ["c1"]
    fake_hub(monkeypatch, dataset, calls, commits)
    monkeypatch.setattr(service, "CACHE", tmp_path / "cache")

    def diagnose():
        response = client.post(
            "/api/levi/diagnostics", json={"repo_id": "org/ds", "max_episodes": 1}
        )
        assert response.status_code == 200, response.text
        assert "cache_note" not in response.json()

    diagnose()
    diagnose()
    assert calls == [("org/ds", "c1", ("meta/**", "data/**/*.parquet"))]
    commits[0] = "c2"  # the dataset changed on the Hub
    diagnose()
    assert [c[1] for c in calls] == ["c1", "c2"]
    # the snapshots are in the size-limited cache, not in the old per-dataset folder
    assert (tmp_path / "cache" / "hub-snapshots").is_dir()
    assert not (tmp_path / "cache" / "org__ds").exists()


@pytest.mark.parametrize(
    ("revision", "sizes", "why"),
    [(None, [10], "no commit from the Hub"), ("r1", [10, None], "size unknown")],
)
def test_without_a_commit_or_a_size_the_snapshot_is_not_cached(
    tmp_path, revision, sizes, why
):
    calls = []
    with take("org/ds", calls, tmp_path, revision=revision, sizes=sizes) as (
        folder,
        note,
    ):
        assert folder == tmp_path / "org__ds"
        assert note.startswith(f"not cached ({why})")
    assert len(calls) == 1


def test_the_reservation_counts_the_listing_of_the_whole_repository():
    assert reserve_for([10], listed=1000) >= reserve_for([10]) + 1000 * 300


def test_an_unreachable_hub_inspects_an_earlier_download(
    client, dataset, tmp_path, monkeypatch
):
    import shutil

    import huggingface_hub
    from huggingface_hub.errors import OfflineModeIsEnabled

    from levi import service

    def offline(self, *args, **kwargs):
        raise OfflineModeIsEnabled("offline")

    monkeypatch.setattr(huggingface_hub.HfApi, "dataset_info", offline)
    monkeypatch.setattr(service, "CACHE", tmp_path / "cache")
    body = {"repo_id": "org/ds", "max_episodes": 1}
    with pytest.raises(OfflineModeIsEnabled):
        client.post("/api/levi/diagnostics", json=body)
    shutil.copytree(dataset, tmp_path / "cache" / "org__ds")
    response = client.post("/api/levi/diagnostics", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["cache_note"].startswith("Hub unreachable")
