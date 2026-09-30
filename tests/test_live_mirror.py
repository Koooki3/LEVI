"""Live service: when a rollout counts as finished, and the mirror that takes
finished ones (and only those) into the workspace."""

import errno
import hashlib
import os
import time
from pathlib import Path

import pytest
from live_helpers import Rollouts

from levi.live import config as live_config
from levi.live import criteria, mirror, sessions


@pytest.fixture
def cfg(tmp_path):
    config = live_config.Config()
    config.service.workspace = str(tmp_path / "ws")
    config.service.home = str(tmp_path / "home")
    config.watch.roots = [str(tmp_path / "rollouts")]
    config.watch.backlog = "process"
    config.watch.settle_s = 0.0
    return config


@pytest.fixture
def rollouts(tmp_path, demo_template):
    return Rollouts(tmp_path / "rollouts", demo_template)


def tree_hash(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path(root).rglob("*"))
        if p.is_file()
    }


def test_a_demo_counts_only_once_every_rule_holds(rollouts):
    now = time.time()
    demo = rollouts.begin(0)
    assert criteria.check(demo, now=now).state == "pending"
    rollouts.finish(0)
    assert criteria.check(demo, now=now).ok
    # Each rule on its own.
    (demo / "metadata.json").write_text('{"stopped_at": ""}')
    assert criteria.check(demo, now=now).state == "pending"
    rollouts.finish(0)
    (demo / ".complete").unlink()
    assert criteria.check(demo, now=now).state == "pending"  # fresh and unmarked
    assert criteria.check(demo, now=now + 3600).ok  # a legacy demo that went quiet


def test_finished_but_unusable_demos_are_rejected_not_retried(rollouts):
    now = time.time()
    rollouts.write(1, stalled=["wrist"])
    rollouts.write(2, match=False)
    assert criteria.check(rollouts.demo(1), now=now).state == "rejected"
    assert "stalled" in criteria.check(rollouts.demo(1), now=now).reason
    assert criteria.check(rollouts.demo(2), now=now).state == "rejected"


def test_scanner_lists_only_finished_demos_and_counts_the_rest(cfg, rollouts):
    rollouts.write(0)
    rollouts.begin(1)
    rollouts.write(2)
    rollouts.incomplete(2, "fr3_fault")
    rollouts.write(3)
    rollouts.discard(3)
    scan = mirror.Scanner(cfg).scan()
    assert len(scan) == 1
    task = scan[0]
    assert task.name == "pi05_fake__stack_the_plates"
    assert task.ready == ["demo_0000"]
    assert task.waiting == ["demo_0001"]
    assert (task.incomplete, task.fr3_fault, task.discarded) == (1, 1, 1)


def test_backlog_is_skipped_unless_asked_for(cfg, rollouts):
    old = rollouts.write(0)
    past = time.time() - 7200
    for path in [old, *old.iterdir()]:
        os.utime(path, (past, past))
    rollouts.write(1)
    cfg.watch.backlog = "skip"
    cfg.watch.since = str(time.time() - 3600)  # the service "began" an hour ago
    task = mirror.Scanner(cfg).scan()[0]
    assert task.ready == ["demo_0001"]
    assert task.backlog == 1
    cfg.watch.backlog = "process"
    assert mirror.Scanner(cfg).scan()[0].ready == ["demo_0000", "demo_0001"]


def test_a_running_session_moves_the_backlog_cutoff_back(cfg, rollouts):
    # Service first started "now", but the evaluation began 10 minutes ago:
    # what it already finished belongs to it, not to the backlog.
    rollouts.write(0)
    cfg.watch.backlog = "skip"
    cfg.watch.since = ""
    rollouts.session("running", started_at=time.time() - 600)
    found = sessions.read_sessions(cfg.watch.roots)
    task = mirror.Scanner(cfg).scan(found)[0]
    assert task.ready == ["demo_0000"]
    # With no session the same demo predates the service's start: backlog.
    (
        Path(cfg.watch.roots[0]) / ".eval_sessions" / "pi05_fake__stack_the_plates.json"
    ).unlink()
    mirror.state_path(cfg, "pi05_fake__stack_the_plates").unlink()
    cfg.watch.since = str(time.time() + 60)
    fresh = mirror.Scanner(cfg).scan({})
    assert fresh[0].ready == [] and fresh[0].backlog == 1


def test_a_session_that_declined_levi_is_left_alone(cfg, rollouts):
    rollouts.write(0)
    rollouts.session("running", levi=False)
    found = sessions.read_sessions(cfg.watch.roots)
    assert mirror.Scanner(cfg).scan(found) == []


def test_mirror_hard_links_finished_demos_and_never_writes_the_source(
    cfg, rollouts, tmp_path
):
    rollouts.write(0)
    before = tree_hash(rollouts.root)
    scan = mirror.Scanner(cfg).scan()[0]
    state = mirror.empty_state(cfg, scan.key, 0.0)
    mirror.jsonio.write(mirror.state_path(cfg, scan.name), state)
    result = mirror.mirror_dataset(cfg, state, scan.ready)
    assert (
        result["demo_0000"]["status"] == "mirrored"
        and result["demo_0000"]["copied"] == 0
    )
    capture = Path(state["capture"])
    src = rollouts.demo(0) / "side_camera.mp4"
    assert os.stat(capture / "demo_0000/side_camera.mp4").st_ino == os.stat(src).st_ino
    assert (capture / "task_description.txt").read_text().strip() == rollouts.text
    assert tree_hash(rollouts.root) == before  # source untouched
    # Idempotent, and the dataset state now knows the demo.
    again = mirror.mirror_dataset(cfg, mirror.load_state(cfg, scan.name), scan.ready)
    assert again["demo_0000"]["status"] == "exists"
    assert (
        mirror.load_state(cfg, scan.name)["demos"]["demo_0000"]["state"] == "mirrored"
    )
    assert mirror.Scanner(cfg).scan()[0].ready == []  # already taken


def test_mirror_copies_when_linking_is_impossible(cfg, rollouts, monkeypatch):
    rollouts.write(0)
    scan = mirror.Scanner(cfg).scan()[0]
    state = mirror.empty_state(cfg, scan.key, 0.0)
    mirror.jsonio.write(mirror.state_path(cfg, scan.name), state)

    def refuse(*args, **kwargs):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(os, "link", refuse)
    result = mirror.mirror_dataset(cfg, state, scan.ready)["demo_0000"]
    assert (
        result["status"] == "mirrored"
        and result["copied"] > 0
        and result["linked"] == 0
    )
    target = Path(state["capture"]) / "demo_0000/side_camera.mp4"
    assert target.read_bytes() == (rollouts.demo(0) / "side_camera.mp4").read_bytes()
    assert (
        os.stat(target).st_ino != os.stat(rollouts.demo(0) / "side_camera.mp4").st_ino
    )


def test_a_demo_that_vanishes_or_changes_while_linking_is_not_mirrored(
    cfg, rollouts, tmp_path
):
    capture = tmp_path / "cap"
    assert (
        mirror.mirror_demo(rollouts.dir / "demo_0099", capture, now=time.time())[
            "status"
        ]
        == "vanished"
    )
    demo = rollouts.write(5)
    (demo / "metadata.json").write_text('{"stopped_at": ""}')  # rewritten just before
    result = mirror.mirror_demo(demo, capture, now=time.time())
    assert result["status"] == "changed"
    assert not (capture / "demo_0005").exists() and not list(capture.glob(".partial-*"))


def test_a_source_that_disappears_is_tolerated(cfg, rollouts):
    rollouts.write(0)
    scanner = mirror.Scanner(cfg)
    assert scanner.scan()[0].available
    import shutil

    shutil.rmtree(rollouts.dir)
    task = scanner.scan()[0]
    assert task.available is False


def test_dataset_names_are_stable_and_distinct():
    assert mirror.dataset_name("pi05", "stack_it") == "pi05__stack_it"
    a, b = mirror.dataset_name("g", "a b"), mirror.dataset_name("g", "a/b")
    assert a != b and all(c.isalnum() or c in "._-" for c in a + b)
    assert mirror.dataset_name("g", "a b") == a
