"""Hub snapshots for diagnostics live in a size-limited DiskBudget (levi/hub_snapshots.py)."""

from levi.hub_snapshots import snapshot

KIB = 1024
GIB = 1024**3


def downloader(calls, size=10 * KIB):
    def download(folder):
        calls.append(str(folder))
        (folder / "meta").mkdir(parents=True, exist_ok=True)
        (folder / "meta" / "info.json").write_bytes(b"x" * size)

    return download


def test_a_snapshot_is_downloaded_once_and_then_reused(tmp_path):
    calls = []
    for _ in range(2):
        with snapshot("org/ds", ["meta/**"], downloader(calls), tmp_path) as (
            folder,
            note,
        ):
            assert (folder / "meta" / "info.json").is_file() and note == ""
    assert len(calls) == 1


def test_different_patterns_are_different_snapshots(tmp_path):
    calls = []
    with snapshot("org/ds", ["meta/**"], downloader(calls), tmp_path):
        pass
    with snapshot("org/ds", ["meta/**", "videos/**"], downloader(calls), tmp_path):
        pass
    assert len(calls) == 2


def test_the_least_recently_used_snapshot_is_evicted_to_stay_under_the_limit(tmp_path):
    cap = 50 * KIB / GIB
    calls = []
    for name in ("org/a", "org/b", "org/c", "org/d", "org/e"):
        with snapshot(
            name, ["meta/**"], downloader(calls, 10 * KIB), tmp_path, cap_gb=cap
        ):
            pass
    with snapshot(
        "org/a", ["meta/**"], downloader(calls, 10 * KIB), tmp_path, cap_gb=cap
    ):
        pass
    # five 10 KiB snapshots plus their bookkeeping do not all fit in 50 KiB: "a" was evicted and fetched again
    assert len(calls) == 6
    total = sum(
        p.stat().st_size for p in (tmp_path / "hub-snapshots").rglob("*") if p.is_file()
    )
    assert total <= 50 * KIB + 8 * KIB


def test_a_snapshot_too_large_for_the_cache_falls_back_to_the_old_folder(tmp_path):
    calls = []
    cap = 20 * KIB / GIB
    with snapshot(
        "org/big",
        ["videos/**"],
        downloader(calls, 40 * KIB),
        tmp_path,
        whole_budget=True,
        cap_gb=cap,
    ) as (folder, note):
        assert folder == tmp_path / "org__big"
        assert (folder / "meta" / "info.json").stat().st_size == 40 * KIB
        assert note.startswith("not cached (too_large)")


def test_a_snapshot_being_inspected_is_never_evicted(tmp_path):
    cap = 30 * KIB / GIB
    calls = []
    with snapshot(
        "org/held", ["meta/**"], downloader(calls, 10 * KIB), tmp_path, cap_gb=cap
    ) as (held, _):
        for name in ("org/b", "org/c", "org/d"):
            with snapshot(
                name, ["meta/**"], downloader(calls, 10 * KIB), tmp_path, cap_gb=cap
            ):
                pass
        assert (held / "meta" / "info.json").stat().st_size == 10 * KIB


def test_a_hub_diagnostic_downloads_once_into_the_budget_and_keeps_its_report(
    client, dataset, tmp_path, monkeypatch
):
    import shutil

    import huggingface_hub

    from levi import service

    calls = []

    def fake_snapshot_download(repo_id, token, repo_type, local_dir, allow_patterns):
        calls.append((repo_id, tuple(allow_patterns)))
        shutil.copytree(dataset, local_dir, dirs_exist_ok=True)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)
    monkeypatch.setattr(service, "CACHE", tmp_path / "cache")
    for _ in range(2):
        response = client.post(
            "/api/levi/diagnostics", json={"repo_id": "org/ds", "max_episodes": 1}
        )
        assert response.status_code == 200, response.text
        assert "cache_note" not in response.json()
    assert calls == [("org/ds", ("meta/**", "data/**/*.parquet"))]
    # the snapshot is in the size-limited cache, not in the old per-dataset folder
    assert (tmp_path / "cache" / "hub-snapshots").is_dir()
    assert not (tmp_path / "cache" / "org__ds").exists()
