"""The gate's history: one line per transition in ``live/gate.jsonl``."""

import json
import time

from test_live_gpu import ctl, live, serve, up  # noqa: F401  (fixtures and helper)

from levi.live import cli, gating


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


def test_transitions_are_recorded_with_their_cause(ctl):  # noqa: F811
    ctl.rollouts.write(0)
    ctl.machine.ports, ctl.machine.policy_mib = {8000}, 7685
    t = time.time()
    ctl.rollouts.session("standby")
    up(ctl, t)
    ctl.rollouts.session("running")
    ctl.tick(t + 1)
    ctl.tick(t + 1.5)  # no change: no new line
    ctl.rollouts.session("homing")
    ctl.tick(t + 2)
    path = ctl.config.live_dir / gating.HISTORY
    rows = lines(path)
    assert [(r["to"]["open"], r["to"]["code"]) for r in rows][-2:] == [
        (False, "policy_inferring"),
        (True, "open"),
    ]
    closed = rows[-2]
    assert closed["from"] == {"open": True, "code": "open"}
    assert closed["policy_up"] is True and closed["policy_ports"]
    assert closed["sessions"] and closed["sessions"][0]["state"] == "running"
    assert closed["reason"] and "time" in closed and closed["at"] > 0
    # The status file shows the last few, and `levi live status` prints them.
    status = ctl.status(t + 2)
    shown = status["gpu"]["gate"]["history"]
    assert shown[-1]["open"] is True and shown[-2]["code"] == "policy_inferring"
    text = cli.format_status({**status, "updated_at": time.time()}, True)
    assert "gate " in text and "CLOSED (policy_inferring)" in text
    # A restarted supervisor remembers them.
    assert [h["to"]["code"] for h in gating.history(ctl.config.live_dir)][-2:] == [
        "policy_inferring",
        "open",
    ]
    # Nothing secret rides along.
    assert "token" not in path.read_text().lower()


def test_shutdown_records_that_the_gate_is_gone(live, serve):  # noqa: F811
    from levi.live import controller

    c, _ = live
    supervisor = controller.Controller(c, log=lambda *a: None)
    supervisor.tick(time.time())
    supervisor.shutdown()
    last = gating.history(c.live_dir, 1)[0]
    assert last["to"] == {"open": None, "code": "service_stopped"}
    assert not (c.live_dir / "gate.json").exists()


def test_history_rotates_and_reads_the_newest(tmp_path):
    folder = tmp_path / "live"
    for n in range(60):
        gating.record_transition(
            folder, {"at": n, "to": {"open": n % 2 == 0, "code": f"c{n}"}}, 500, keep=2
        )
    names = sorted(p.name for p in folder.iterdir())
    assert names == ["gate.jsonl", "gate.jsonl.1", "gate.jsonl.2"]
    assert (folder / "gate.jsonl").stat().st_size <= 500 + 200
    got = gating.history(folder, 5)
    assert [r["to"]["code"] for r in got] == [f"c{n}" for n in range(55, 60)]
    # A short current file continues from the rotated one.
    newest = lines(folder / "gate.jsonl")
    assert len(gating.history(folder, len(newest) + 2)) == len(newest) + 2


def test_a_damaged_history_is_read_without_crashing(tmp_path):
    folder = tmp_path / "live"
    assert gating.history(folder) == []  # no file
    folder.mkdir()
    path = folder / gating.HISTORY
    good = json.dumps({"at": 1, "to": {"open": False, "code": "policy_inferring"}})
    path.write_bytes(
        b"not json\n" + good.encode() + b"\n\xff\xfe\n[1, 2]\n"
        b'{"at": 2, "to": "oops"}\n{"at": 3, "to'
    )
    got = gating.history(folder)
    assert [r["at"] for r in got] == [1, 2]
    assert gating.brief(got[1]) == {"at": 2, "open": None, "code": None, "reason": ""}
    # Appending still works after the torn tail, and never raises on a bad row.
    gating.record_transition(folder, {"at": 4, "to": {}})
    gating.record_transition(folder, {"bad": object()})  # default=str keeps it
    gating.record_transition(folder / gating.HISTORY / "nope", {"at": 5})  # unwritable
