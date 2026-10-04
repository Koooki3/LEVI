"""One task under two rollout roots is two datasets, and a worker that finds
nothing does not spin.

A test evaluation had been registered as ``<group>__<task>``; the real one,
under another root, got the same name, inherited the old state (whose source
was a cleared folder) and no episode of it was ever mirrored: the supervisor
started a worker every half second that found nothing to do."""

import hashlib
import os
import shutil
import time
from pathlib import Path

from live_helpers import Rollouts
from test_live_pipeline import env  # noqa: F401  (fixture)

from levi.live import cli, controller, gpumgr, mirror
from levi.live import config as live_config

GROUP, TASK = "pi05_fr3_all_state", "pick_the_eggplant_in_the_blue_plate"
BASE = f"{GROUP}__{TASK}"


def config_for(tmp_path, roots):
    c = live_config.Config()
    c.service.workspace = str(tmp_path / "ws")
    c.service.home = str(tmp_path / "home")
    c.watch.roots = [str(r) for r in roots]
    c.watch.backlog = "process"
    c.watch.settle_s = 0.0
    c.gpu.mode = "manual"
    c.gpu.lock_file = ""
    cli.prepare(c)
    return c


def rollouts(tmp_path, demo_template, *parts):
    root = tmp_path.joinpath(*parts)
    return Rollouts(root, demo_template, group=GROUP, task=TASK)


def names_by_root(config):
    return {t.key[0]: t.name for t in mirror.Scanner(config).scan()}


def test_the_plain_name_stays_with_the_root_that_holds_it(tmp_path, demo_template):
    old = rollouts(tmp_path, demo_template, "online_rollout_data_test")
    new = rollouts(tmp_path, demo_template, "online_rollout_data", "models")
    old.write(0)
    new.write(0)
    c = config_for(tmp_path, [old.root])
    assert names_by_root(c) == {str(old.root): BASE}
    assert mirror.load_state(c, BASE)["root"] == str(old.root)
    # Now both are watched, the new root first. Nothing about the old name moves.
    c.watch.roots = [str(new.root), str(old.root)]
    got = names_by_root(c)
    assert got[str(old.root)] == BASE
    assert got[str(new.root)] == f"{BASE}__at__models"
    # The new one has its own state, source and capture folder.
    fresh = mirror.load_state(c, f"{BASE}__at__models")
    assert fresh["root"] == str(new.root) and fresh["source"] == str(new.dir)
    assert fresh["capture"] != mirror.load_state(c, BASE)["capture"]
    assert fresh["capture"].endswith(f"{BASE}__at__models")
    # A restart (a new scanner, states on disk) chooses the same names.
    assert names_by_root(c) == got
    c.watch.roots = [str(old.root), str(new.root)]
    assert names_by_root(c) == got


def test_roots_with_the_same_last_folder_name_still_get_distinct_names(
    tmp_path, demo_template
):
    roots = [
        rollouts(tmp_path, demo_template, "a", "models"),
        rollouts(tmp_path, demo_template, "b", "models"),
        rollouts(tmp_path, demo_template, "c", "models"),
    ]
    for r in roots:
        r.write(0)
    c = config_for(tmp_path, [r.root for r in roots])
    got = names_by_root(c)
    assert len(set(got.values())) == 3, got
    assert got[str(roots[0].root)] == BASE
    assert got[str(roots[1].root)] == f"{BASE}__at__models"
    digest = hashlib.sha256(os.path.realpath(roots[2].root).encode()).hexdigest()[:6]
    assert got[str(roots[2].root)] == f"{BASE}__at__models-{digest}"
    assert names_by_root(c) == got  # and again after a restart
    # Every name is one the live API and the catalog accept.
    from levi.live import api

    assert all(api.NAME.match(n) for n in got.values())


def test_a_very_long_name_stays_within_what_the_api_accepts(tmp_path, demo_template):
    from levi.live import api

    long_task = "t" * 130
    for tag in ("x" * 60, "models"):
        name = mirror.resolve_name(
            config_for(tmp_path, []), tmp_path / tag, "g" * 10, long_task
        )
        assert api.NAME.match(name)
    name = mirror.resolve_name(
        config_for(tmp_path, []), tmp_path / ("x" * 60), "g", long_task, {}
    )
    # (the plain name is used until something holds it)
    assert len(name) <= 140


def test_a_readers_name_is_the_one_the_scanner_chose(tmp_path, demo_template):
    old = rollouts(tmp_path, demo_template, "old")
    new = rollouts(tmp_path, demo_template, "new")
    old.write(0)
    c = config_for(tmp_path, [old.root])
    names_by_root(c)
    new.write(0)
    c.watch.roots = [str(old.root), str(new.root)]
    scanner = mirror.Scanner(c)
    scanner.scan()
    for root, expected in ((old.root, BASE), (new.root, f"{BASE}__at__new")):
        key = (str(root), GROUP, TASK)
        assert scanner.known_name(key) == scanner.name_of(key) == expected
        assert mirror.resolve_name(c, str(root), GROUP, TASK) == expected


def tree(path):
    return sorted(
        (str(p.relative_to(path)), p.stat().st_size)
        for p in Path(path).rglob("*")
        if p.is_file()
    )


def test_a_second_root_is_labelled_beside_the_old_dataset_not_over_it(
    env,  # noqa: F811
    tmp_path,
    demo_template,
):
    e = env()
    e.rollouts.write(0)
    e.rollouts.write(1)
    e.run()
    old_state = e.state()
    assert {d: r["state"] for d, r in old_state["demos"].items()} == {
        "demo_0000": "done",
        "demo_0001": "done",
    }
    capture = Path(old_state["capture"])
    before = tree(capture)
    # The old evaluation's folder is cleared, and the real one starts under
    # another root, with the same group, task and demo numbers.
    shutil.rmtree(e.rollouts.dir)
    real = Rollouts(
        tmp_path / "online_rollout_data" / "models",
        demo_template,
        group="pi05_fake",
        task="stack_the_plates",
        text="stack the plates of same color together",
    )
    for n in (0, 1, 2):
        real.write(n)
    e.config.watch.roots = [str(real.root), str(e.rollouts.root)]
    e.run()
    new_name = f"{NAME_}__at__models"
    new_state = e.state(new_name)
    assert new_state and new_state["source"] == str(real.dir)
    assert {d: r["state"] for d, r in new_state["demos"].items()} == {
        "demo_0000": "done",
        "demo_0001": "done",
        "demo_0002": "done",
    }
    # The old dataset is exactly as it was: same source record, same demos,
    # and the capture folder (the only copy) untouched.
    after = e.state()
    assert after["source"] == old_state["source"] and after["root"] == old_state["root"]
    assert after["capture"] == old_state["capture"]
    assert {d: r["state"] for d, r in after["demos"].items()} == {
        "demo_0000": "done",
        "demo_0001": "done",
    }
    assert tree(capture) == before
    new_capture = Path(new_state["capture"])
    assert new_capture != capture and new_capture.is_dir()
    assert sorted(p.name for p in new_capture.glob("demo_*")) == [
        "demo_0000",
        "demo_0001",
        "demo_0002",
    ]
    # Each dataset has its own annotations for the same demo number.
    assert len(e.atoms(0, NAME_)) == 5 and len(e.atoms(0, new_name)) == 5
    assert len(e.atoms(2, new_name)) == 5


NAME_ = "pi05_fake__stack_the_plates"


class Exits:
    """A worker process that exits at once with ``code``."""

    pid = 4242

    def __init__(self, code):
        self.code = code

    def poll(self):
        return self.code


def no_model_server_anywhere(monkeypatch):
    """As on CI or a machine whose vLLM is down: nothing answers on any port.
    These tests once passed only because a real vLLM answered on :8100 (and
    failed when it was down or too slow to answer its health check under
    load)."""
    monkeypatch.setattr(gpumgr, "healthy", lambda *a, **k: False)


def supervisor(c, code=14):
    """A supervisor in manual GPU mode whose model server is "up" (a stand-in,
    no socket is opened) and whose workers exit at once with ``code``."""
    made = []

    def popen(*args, **kwargs):
        made.append(Exits(code))
        return made[-1]

    messages = []
    ctl = controller.Controller(
        c,
        probes=controller.Probes(
            vram=lambda: None,
            ports=lambda: set(),
            holders=list,
            policy_vram=lambda p: None,
        ),
        popen=popen,
        log=lambda *a: messages.append(" ".join(map(str, a))),
    )
    ctl.vllm.external = lambda: True
    return ctl, made, messages


def test_a_worker_that_finds_nothing_is_not_restarted_every_half_second(
    tmp_path, demo_template, monkeypatch
):
    no_model_server_anywhere(monkeypatch)
    r = rollouts(tmp_path, demo_template, "root")
    r.write(0)
    c = config_for(tmp_path, [r.root])
    ctl, made, messages = supervisor(c)
    t = time.time()
    for i in range(240):  # two minutes of half-second ticks
        ctl.tick(t + i * 0.5)
    assert 3 <= len(made) <= 12, len(made)
    started = [m for m in messages if "batch started" in m]
    nothing = [m for m in messages if "nothing to do" in m]
    assert len(started) == 1, messages
    assert 1 <= len(nothing) <= 6 and "in a row" in nothing[-1]
    # A worker that finishes a batch (exit 0) ends the streak.
    ctl.nothing.clear()
    ctl.worker = Exits(0)
    ctl.worker_dataset = BASE
    ctl._reaped(0)
    assert BASE not in ctl.nothing


def test_a_dataset_whose_source_is_gone_is_marked_and_left_until_it_returns(
    tmp_path, demo_template, monkeypatch
):
    no_model_server_anywhere(monkeypatch)
    r = rollouts(tmp_path, demo_template, "root")
    r.write(0)
    c = config_for(tmp_path, [r.root])
    names_by_root(c)  # creates the state
    name = BASE
    gone = tmp_path / "cleared" / GROUP / TASK
    gone.mkdir(parents=True)  # exists but empty
    shutil.rmtree(r.root)

    def point(value):
        value["source"] = str(gone)
        value["current"] = {"demos": ["demo_0000"], "temporal": {}}  # work "due"
        return value

    from levi.live import jsonio

    jsonio.update(mirror.state_path(c, name), point, default=dict)
    ctl, made, messages = supervisor(c)
    t = time.time()
    for i in range(40):
        ctl.tick(t + i * 0.5)
    assert len(made) == 1, len(made)  # one try, then it is left alone
    state = mirror.load_state(c, name)
    assert "empty" in state["unavailable"]["reason"]
    row = ctl.dataset_rows(t + 20)[name]
    assert row["available"] is False and "empty" in row["unavailable_reason"]
    assert any("not labelling it until it is back" in m for m in messages)
    # The source comes back: the mark clears and the dataset is queued again.
    (gone / "demo_0000").mkdir()
    assert mirror.is_available(c, name)
    assert "unavailable" not in mirror.load_state(c, name)
    assert name in ctl._queue(t + 100)
