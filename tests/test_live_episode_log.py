"""The worker writes one greppable summary line per labelled demo."""

import re

from test_live_pipeline import NAME, env  # noqa: F401  (fixture)

from levi.live import worker


def test_model_use_counts_requests_tokens_and_seconds_of_one_episode():
    events = [
        {
            "type": "model_step",
            "episode": 3,
            "usage": {"tokens": 100, "elapsed_seconds": 2.0},
        },
        {
            "type": "model_step",
            "episode": 3,
            "usage": {"tokens": 50, "elapsed_seconds": 0.5, "cached": True},
        },
        {
            "type": "model_step",
            "episode": 4,
            "usage": {"tokens": 999, "elapsed_seconds": 9.0},
        },
        {"type": "shard_completed", "episode": 3},
        {"type": "model_step", "episode": 3, "usage": {}},
    ]
    assert worker.model_use(events, 3) == {"requests": 2, "tokens": 150, "model_s": 2.5}
    assert worker.model_use(events, 9) == {"requests": 0, "tokens": 0, "model_s": 0.0}


def test_the_line_names_every_figure_and_a_gate_interruption():
    use = {"requests": 2, "tokens": 21000, "model_s": 10.6}
    row = {
        "state": "done",
        "temporal": {"segments": 3},
        "verdict": {"outcome": "failure", "valid_events": 0, "undecided": False},
    }
    line = worker.episode_line(
        "demo_0003",
        3,
        2,
        {"temporal": (28.3, use), "review": (6.0, use)},
        row,
        [1, 5.25],
    )
    assert line == (
        "episode demo=demo_0003 ep=3 batch=2 "
        "temporal_wall=28.3s temporal_model=10.6s temporal_requests=2 temporal_tokens=21000 "
        "review_wall=6.0s review_model=10.6s review_requests=2 review_tokens=21000 "
        "segments=3 verdict=failure valid_events=0 gated=1x/5.2s state=done"
    )
    failed = worker.episode_line(
        "demo_0004", 4, 1, {}, {"state": "mirrored", "reason": "model: boom"}, [0, 0.0]
    )
    assert (
        "segments=0 verdict=none gated=no state=mirrored reason='model: boom'" in failed
    )


def test_a_finished_batch_leaves_one_line_per_demo_in_worker_log(env):  # noqa: F811
    e = env()
    e.rollouts.write(0)
    e.rollouts.write(1)
    e.run()
    log = (e.ws / "live/logs/worker.log").read_text()
    lines = [x for x in log.splitlines() if " episode demo=" in x]
    assert len(lines) == 2, log
    for number, line in enumerate(lines):
        assert f"demo=demo_{number:04d} " in line and "batch=2" in line
        assert re.search(r"temporal_wall=\d+\.\d+s", line)
        assert re.search(r"temporal_requests=[1-9]\d* temporal_tokens=\d+", line)
        assert re.search(r"review_requests=[1-9]\d* ", line)
        assert re.search(r"segments=[1-9]", line)
        assert "verdict=" in line and "gated=no" in line and "state=done" in line


def test_a_resumed_batch_keeps_what_earlier_workers_spent():
    import pytest

    def fresh(batch):
        w = worker.Worker.__new__(worker.Worker)
        w.walls = dict(batch.get("walls") or {})
        w.gated = list(batch.get("gated") or [0, 0.0])
        w.save_current = lambda b: None
        return w

    batch = {}
    first = fresh(batch)

    def stopped():
        first.gated[0] += 1
        first.gated[1] += 2.5
        raise worker.Stop

    with pytest.raises(worker.Stop):
        first.timed(batch, "temporal", stopped)  # stopped mid-phase: still kept
    assert batch["gated"] == [1, 2.5] and batch["walls"]["temporal"] >= 0
    before = batch["walls"]["temporal"]
    second = fresh(batch)  # the next worker process continues the batch
    second.timed(batch, "temporal", lambda: second.gated.__setitem__(0, 2))
    second.timed(batch, "review", lambda: None)
    assert batch["walls"]["temporal"] >= before and "review" in batch["walls"]
    assert batch["gated"][0] == 2 and batch["gated"][1] == 2.5


def test_one_demo_failing_to_log_does_not_cost_the_others_their_records(tmp_path):
    from levi.live import config as live_config
    from levi.live import stats

    c = live_config.Config()
    c.service.workspace = str(tmp_path / "ws")
    w = worker.Worker.__new__(worker.Worker)
    w.config, w.view, w.store = c, None, None
    w.walls, w.gated = {"temporal": 1.0}, [0, 0.0]
    w.state = lambda: {"demos": {}}
    written = []

    def log(line):
        if "demo_0000" in line:
            raise RuntimeError("boom")
        written.append(line)

    w.log = log
    w.stats_row = lambda demo, *a, **k: {"demo": demo}
    w.log_episodes({"demos": ["demo_0000", "demo_0001", "demo_0002"]}, {})
    assert [r["demo"] for r in stats.read(c.live_dir)] == [
        "demo_0000",
        "demo_0001",
        "demo_0002",
    ]
    assert len(written) == 2
    # And a record failing does not stop the next one or the log lines.
    n = stats.read(c.live_dir)
    w.stats_row = lambda demo, *a, **k: 1 / 0
    w.log_episodes({"demos": ["demo_0003"]}, {})
    assert stats.read(c.live_dir) == n and len(written) == 3
