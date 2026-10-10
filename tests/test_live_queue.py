"""The background labelling queue (``levi/live/queue.py``): the registry of
labelling modes, history for one dataset, the order work is taken in, and the
status rows. No GPU, no model."""

import os
import time

import pytest
from live_helpers import Rollouts

from levi.live import cli, controller, mirror
from levi.live import config as live_config
from levi.live import queue as lq

NAME = "pi05_fake__stack_the_plates"


@pytest.fixture
def cfg(tmp_path):
    c = live_config.Config()
    c.service.workspace = str(tmp_path / "ws")
    c.service.home = str(tmp_path / "home")
    c.watch.roots = [str(tmp_path / "rollouts")]
    c.watch.backlog = "skip"
    c.watch.since = str(time.time() - 3600)
    c.watch.settle_s = 0.0
    c.gpu.lock_file = str(tmp_path / "gpu.lock")
    cli.prepare(c)
    return c


@pytest.fixture
def rollouts(tmp_path, demo_template):
    return Rollouts(tmp_path / "rollouts", demo_template)


def old_demos(rollouts, numbers, age=7200):
    past = time.time() - age
    for n in numbers:
        demo = rollouts.write(n)
        for path in [demo, *demo.iterdir()]:
            os.utime(path, (past, past))


# --- the registry -------------------------------------------------------------


def test_a_mode_is_saved_and_read_back_by_a_second_reader(cfg):
    assert lq.mode_of(cfg, NAME) == "auto"  # nothing registered: the default
    done = lq.set_mode(cfg, NAME, "history", by="person", request_id="r1")
    assert done["changed"] and done["previous"] == "auto" and not done["replayed"]
    assert lq.mode_of(cfg, NAME) == "history"
    assert lq.listing(cfg)["datasets"][NAME]["mode"] == "history"
    # A reader that starts fresh (another process, after a restart).
    lq._CACHE.clear()
    assert lq.mode_of(cfg, NAME) == "history"
    assert not list(cfg.live_dir.glob(".queue.json.*.tmp"))


def test_the_default_mode_comes_from_the_configuration(cfg):
    cfg.queue.default_mode = "paused"
    assert lq.mode_of(cfg, "something") == "paused"
    assert not lq.processes(cfg, "something")
    assert lq.register(cfg, ["a", "b"]) == ["a", "b"]
    assert lq.register(cfg, ["a", "b", "c"]) == ["c"]
    assert lq.register(cfg, ["a"]) == []
    assert lq.listing(cfg)["datasets"]["a"]["by"] == "default"


def test_the_same_request_twice_changes_nothing_twice(cfg):
    first = lq.set_mode(cfg, NAME, "paused", request_id="abc")
    stamp = lq.path(cfg).stat().st_mtime_ns
    again = lq.set_mode(cfg, NAME, "paused", request_id="abc")
    assert first["changed"] and again["replayed"] and not again["changed"]
    assert again["previous"] == "auto"
    assert lq.path(cfg).stat().st_mtime_ns == stamp
    # Somebody else's change in between does not make the replay change it back.
    lq.set_mode(cfg, NAME, "off", request_id="other")
    assert lq.set_mode(cfg, NAME, "paused", request_id="abc")["replayed"]
    assert lq.mode_of(cfg, NAME) == "off"
    with pytest.raises(lq.Conflict):
        lq.set_mode(cfg, NAME, "auto", request_id="abc")
    # Without a request id the same mode is simply unchanged.
    assert not lq.set_mode(cfg, NAME, "off")["changed"]


def test_a_bad_mode_or_name_is_refused_and_nothing_is_written(cfg):
    with pytest.raises(lq.Invalid):
        lq.set_mode(cfg, NAME, "sometimes")
    with pytest.raises(lq.Invalid):
        lq.set_mode(cfg, "", "auto")
    assert not lq.path(cfg).exists()


def test_a_damaged_file_is_refused_kept_and_reads_as_paused(cfg):
    lq.set_mode(cfg, NAME, "auto")
    file = lq.path(cfg)
    for damage in (
        "{not json",
        '{"schema": "other"}',
        '{"schema": "levi.live.queue.v1", "datasets": {"x": {"mode": "zzz"}}}',
    ):
        file.write_text(damage)
        before = file.read_bytes()
        with pytest.raises(lq.Unreadable):
            lq.set_mode(cfg, NAME, "history")
        with pytest.raises(lq.Unreadable):
            lq.register(cfg, ["new"])
        assert file.read_bytes() == before  # the original is kept
        modes, problem = lq.load(cfg)
        assert problem and modes == {}
        assert lq.mode_of(cfg, NAME) == "paused"  # nothing is labelled on a guess
        assert not lq.processes(cfg, NAME)


def test_concurrent_writers_lose_no_change(cfg):
    import threading

    names = [f"d{i}" for i in range(12)]
    errors = []

    def put(name):
        try:
            lq.set_mode(cfg, name, "paused")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=put, args=(n,)) for n in names]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert set(lq.listing(cfg)["datasets"]) == set(names)


# --- history and order -----------------------------------------------------------


def test_history_is_labelled_only_for_the_dataset_that_asks_for_it(cfg, rollouts):
    old_demos(rollouts, [0, 1, 2])
    rollouts.write(3)
    task = mirror.Scanner(cfg).scan()[0]
    assert task.ready == ["demo_0003"] and task.backlog == 3
    # One dataset opts in: its past counts, newest first, behind the new one.
    lq.set_mode(cfg, NAME, "history")
    task = mirror.Scanner(cfg).scan()[0]
    assert task.ready == ["demo_0003", "demo_0002", "demo_0001", "demo_0000"]
    assert task.backlog == 0
    # The global setting is untouched and ``[queue] history = off`` ignores it.
    assert cfg.watch.backlog == "skip"
    cfg.queue.history = "off"
    assert mirror.Scanner(cfg).scan()[0].backlog == 3


def test_a_running_scanner_notices_the_mode_changing(cfg, rollouts):
    old_demos(rollouts, [0, 1])
    rollouts.write(2)
    scanner = mirror.Scanner(cfg)
    assert scanner.scan()[0].ready == ["demo_0002"]
    lq.set_mode(cfg, NAME, "history")
    assert scanner.scan()[0].ready == ["demo_0002", "demo_0001", "demo_0000"]
    lq.set_mode(cfg, NAME, "auto")
    assert scanner.scan()[0].ready == ["demo_0002"]


def test_oldest_first_keeps_the_folder_order(cfg, rollouts):
    cfg.queue.order = "oldest_first"
    old_demos(rollouts, [0, 1])
    lq.set_mode(cfg, NAME, "history")
    assert mirror.Scanner(cfg).scan()[0].ready == ["demo_0000", "demo_0001"]
    assert lq.order_demos(cfg, NAME, ["demo_0002", "demo_0000"]) == [
        "demo_0000",
        "demo_0002",
    ]
    cfg.queue.order = "newest_first"
    assert lq.order_demos(cfg, NAME, ["demo_0002", "demo_0000"]) == [
        "demo_0002",
        "demo_0000",
    ]
    lq.set_mode(cfg, NAME, "auto")
    assert lq.order_demos(cfg, NAME, ["demo_0002", "demo_0000"]) == [
        "demo_0000",
        "demo_0002",
    ]


def test_the_batch_size_follows_the_queue_setting(cfg):
    assert cfg.batch_limit() == cfg.watch.batch_max_episodes
    cfg.queue.batch_max = 7
    assert cfg.batch_limit() == 7


# --- the supervisor's queue --------------------------------------------------------


def make_controller(cfg):
    ctl = controller.Controller(
        cfg,
        probes=controller.Probes(
            vram=lambda: {"total_mib": 32607, "used_mib": 1000, "free_mib": 31607},
            ports=lambda: set(),
            holders=list,
            policy_vram=lambda ports: None,
        ),
        log=lambda *a: None,
    )
    return ctl


def second_task(rollouts, tmp_path, demo_template):
    other = Rollouts(
        tmp_path / "rollouts", demo_template, group="pi05_fake", task="other_task"
    )
    return other


def test_new_datasets_are_registered_and_a_paused_one_is_not_labelled(
    cfg, rollouts, tmp_path, demo_template
):
    rollouts.write(0)
    other = second_task(rollouts, tmp_path, demo_template)
    other.write(0)
    ctl = make_controller(cfg)
    try:
        now = time.time()
        ctl._refresh(now, True)
        ctl._register_datasets(now)
        rows = lq.listing(cfg)["datasets"]
        assert {NAME, "pi05_fake__other_task"} <= set(rows)
        assert {r["mode"] for r in rows.values()} == {"auto"}
        assert sorted(ctl._queue(now)) == sorted(rows)
        lq.set_mode(cfg, "pi05_fake__other_task", "paused")
        assert ctl._queue(now) == [NAME]
        lq.set_mode(cfg, NAME, "off")
        assert ctl._queue(now) == []
    finally:
        ctl.shutdown()


def test_auto_datasets_go_before_history_whatever_waited_longer(
    cfg, rollouts, tmp_path, demo_template
):
    rollouts.write(0)
    second_task(rollouts, tmp_path, demo_template).write(0)
    ctl = make_controller(cfg)
    try:
        now = time.time()
        ctl._refresh(now, True)
        ctl._register_datasets(now)
        lq.set_mode(cfg, "pi05_fake__other_task", "auto")
        lq.set_mode(cfg, NAME, "history")
        # The history dataset has waited longer (never processed), the auto
        # one is taken first anyway.
        assert ctl._queue(now) == ["pi05_fake__other_task", NAME]
    finally:
        ctl.shutdown()


def test_a_damaged_registry_stops_the_labelling_and_says_so_once(cfg, rollouts):
    rollouts.write(0)
    ctl = make_controller(cfg)
    try:
        now = time.time()
        ctl._refresh(now, True)
        ctl._register_datasets(now)
        lq.path(cfg).write_text("{broken")
        ctl._registered.clear()
        ctl._register_datasets(now + 1)
        ctl._register_datasets(now + 2)
        assert ctl._queue(now) == []
        said = [e for e in ctl.events if "labelling queue" in e["text"]]
        assert len(said) == 1 and said[0]["level"] == "error"
        assert lq.path(cfg).read_text() == "{broken"
        rows = ctl.queue_status(now, ctl.dataset_rows(now))
        assert rows[0]["state"] == "paused" and "queue.json" in rows[0]["reason"]
    finally:
        ctl.shutdown()


def test_status_rows_name_the_state_of_every_dataset(cfg):
    lq.set_mode(cfg, "p", "paused")
    lq.set_mode(cfg, "o", "off")
    lq.set_mode(cfg, "h", "history")
    rows = lq.status_rows(
        cfg,
        names=["a", "b", "c", "d", "p", "o", "h"],
        waiting={"a": 3, "b": 2, "c": 0, "d": 5, "h": 9},
        running="a",
        failed={"b": "retry in 30 s"},
        yielding="a policy server is up",
    )
    by = {r["dataset"]: r for r in rows}
    assert by["a"]["state"] == "running" and by["a"]["waiting"] == 3
    assert by["b"]["state"] == "failed" and "30" in by["b"]["reason"]
    assert by["c"]["state"] == "done"
    assert by["d"]["state"] == "yielding" and "policy" in by["d"]["reason"]
    assert by["p"]["state"] == "paused" and by["o"]["state"] == "paused"
    assert by["h"]["state"] == "yielding" and by["h"]["mode"] == "history"
    assert {r["state"] for r in rows} <= set(lq.STATES)
    # Auto before history before the rest.
    assert [r["mode"] for r in rows][:4] == ["auto"] * 4
    assert rows[4]["mode"] == "history"
    quiet = lq.status_rows(
        cfg, names=["d"], waiting={"d": 5}, running=None, failed={}, yielding=None
    )
    assert quiet[0]["state"] == "queued"


# --- the configuration -------------------------------------------------------------


def test_the_default_file_is_what_older_readers_accept_and_init_writes_the_new_tables():
    import tomllib

    plain = live_config.render()
    assert "[queue]" not in plain and "[gpu.yield]" not in plain
    full = live_config.render(full=True)
    assert "[queue]" in full and "[gpu.yield]" in full
    data = tomllib.loads(full)
    assert data["gpu"]["yield"]["resume_after_s"] == 60.0
    config = live_config.from_dict(data)
    assert config.gpu.yield_ == live_config.GpuYield()
    assert config.queue == live_config.Queue()
    # A table that differs from its default is always written.
    config.queue.order = "oldest_first"
    config.gpu.yield_.foreign_gpu_mib = 500
    text = live_config.render(config)
    assert 'order = "oldest_first"' in text and "foreign_gpu_mib = 500" in text
    again = live_config.from_dict(tomllib.loads(text))
    assert (
        again.queue.order == "oldest_first" and again.gpu.yield_.foreign_gpu_mib == 500
    )


@pytest.mark.parametrize(
    "toml",
    [
        '[queue]\ndefault_mode = "history"',
        '[queue]\nhistory = "always"',
        '[queue]\norder = "random"',
        "[queue]\nbatch_max = -1",
        "[gpu.yield]\nresume_after_s = -1",
        "[gpu.yield]\nforeign_gpu_mib = -5",
        "[gpu.yield]\nunknown = 1",
        "[queue]\nunknown = 1",
        "[gpu]\nyield_ = {}",
    ],
)
def test_bad_queue_and_yield_settings_are_errors(toml):
    import tomllib

    with pytest.raises(ValueError):
        live_config.from_dict(tomllib.loads(toml))
