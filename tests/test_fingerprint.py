"""``fingerprint`` must keep the digest of the ``rglob`` version it replaced:
built views store it, and any change would make every raw capture look
modified and rebuild its view."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from levi.conversion.engine import fingerprint


def rglob_fingerprint(source: Path):
    """The implementation before 2026-10-30, kept as the reference."""
    rows = []
    for p in sorted(source.rglob("*")):
        rel = p.relative_to(source).as_posix()
        if p.is_symlink():
            rows.append((rel, "link", os.readlink(p)))
        elif p.is_file():
            st = p.stat()
            rows.append((rel, st.st_size, st.st_mtime_ns))
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


@pytest.fixture
def capture(tmp_path):
    root = tmp_path / "capture"
    for rel in (
        "a/b",
        "a-c",
        "a.b",
        "A",
        ".hidden",
        "demo_10/cam/0001.jpg",
        "demo_2/cam/0001.jpg",
        "demo_2/metadata.json",
        "演示/数据.csv",
        "deep/x/y/z/leaf.bin",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rel.encode() * 3)
    (root / "empty").mkdir()
    outside = tmp_path / "outside"
    (outside / "inner").mkdir(parents=True)
    (outside / "inner" / "f.txt").write_text("not walked")
    (root / "linked_dir").symlink_to(outside)
    (root / "linked_file").symlink_to(root / "a-c")
    (root / "dangling").symlink_to(tmp_path / "missing")
    return root


def test_digest_matches_the_rglob_version(capture):
    assert fingerprint(capture) == rglob_fingerprint(capture)


def test_digest_still_sees_a_change(capture):
    before = fingerprint(capture)
    (capture / "demo_2" / "metadata.json").write_bytes(b"changed, longer content")
    assert fingerprint(capture) != before
    assert fingerprint(capture) == rglob_fingerprint(capture)


def test_symlinked_directory_is_listed_not_entered(capture, tmp_path):
    before = fingerprint(capture)
    (tmp_path / "outside" / "inner" / "f.txt").write_text("changed outside")
    assert fingerprint(capture) == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_unreadable_directory_is_skipped_like_rglob(capture):
    locked = capture / "deep" / "x"
    locked.chmod(0)
    try:
        assert fingerprint(capture) == rglob_fingerprint(capture)
    finally:
        locked.chmod(0o755)


def test_empty_missing_and_file_source(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    file = tmp_path / "file.txt"
    file.write_text("x")
    for source in (empty, tmp_path / "missing", file):
        assert fingerprint(source) == rglob_fingerprint(source)


def test_symlinked_source_is_walked(capture, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(capture)
    assert fingerprint(alias) == rglob_fingerprint(alias) == fingerprint(capture)


def test_a_file_that_vanishes_during_the_walk_is_skipped_like_rglob(
    capture, monkeypatch
):
    # The entry is listed (its type known from the listing) and gone before
    # its stat: rglob's is_file() swallowed that; the walk must skip it too.
    gone = capture / "demo_2" / "metadata.json"
    from levi.conversion import engine

    original_scandir = os.scandir

    def scandir_then_delete(path):
        it = original_scandir(path)
        if os.fspath(path).endswith("demo_2"):
            entries = list(it)
            it.close()
            gone.unlink()

            class Listed:
                def __enter__(self):
                    return iter(entries)

                def __exit__(self, *exc):
                    return False

            return Listed()
        return it

    monkeypatch.setattr(engine.os, "scandir", scandir_then_delete)
    digest = fingerprint(capture)
    monkeypatch.undo()
    assert digest == rglob_fingerprint(capture)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_a_source_under_an_unsearchable_parent_raises_like_rglob(tmp_path):
    parent = tmp_path / "locked"
    source = parent / "capture"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("x")
    parent.chmod(0o600)  # readable, not searchable
    try:
        with pytest.raises(PermissionError):
            rglob_fingerprint(source)
        with pytest.raises(PermissionError):
            fingerprint(source)
    finally:
        parent.chmod(0o755)
