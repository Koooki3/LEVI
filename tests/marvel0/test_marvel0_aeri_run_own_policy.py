"""A start of terminal D stops the step-4 policy server this page deployed
(L2-STEP5-SIMPLIFY-1117, item 2): the two readiness rows (``ports_free``,
``no_policy_deploy``) say ``warn`` with the page's own unit named, the start
stops that unit through the deploy supervisor's own stop, reads everything again
and goes on only if every row then passes without the exception. Anything else
on the ports still fails readiness, and a stop that fails, or does not end idle,
or leaves the rows as they were, means D is not started.

Nothing is stopped for real: ``Host.stop_own_policy`` is a fake that records the
call, and a test of the real function drives it with a fake deploy supervisor."""

import types

import pytest
from aeri_machine import NAME
from aeri_run_kit import machine_of, refusal

from levi.marvel0 import aeri_run, deploy_api

UNIT = "levi-aeri-policy-manual-marvel0-pi05"
OK = {"code": "stack_ready", "level": "warn", "params": {}}


def rows(own=True, extra=()):
    own_params = {"own_policy": UNIT, "unit": UNIT}
    return [
        {"code": "gpu_free", "level": "ok", "params": {}},
        {
            "code": "ports_free",
            "level": "warn" if own else "ok",
            "params": {"ports": [8000], **(own_params if own else {})},
        },
        {
            "code": "no_policy_deploy",
            "level": "warn" if own else "ok",
            "params": {"state": "ready", **(own_params if own else {})},
        },
        OK,
        *extra,
    ]


def snap(own=True, sha="snap-own", extra=()):
    return {"rows": rows(own, extra), "ready": True, "sha256": sha}


@pytest.fixture(name="m")
def machine(tmp_path, monkeypatch):
    mm = machine_of(tmp_path / "a1", monkeypatch)
    mm.events = []
    mm.stop_calls = []
    mm.stop_answer = {"state": "idle", "stopped": True, "unit": UNIT}
    mm.after_stop = lambda: snap(own=False, sha="snap-clear")

    def stop(request_id):
        mm.events.append("stop")
        mm.stop_calls.append(request_id)
        if isinstance(mm.stop_answer, Exception):
            raise mm.stop_answer
        mm.snap = mm.after_stop()
        return mm.stop_answer

    mm.host.stop_own_policy = stop
    real_spawn = mm.host.spawn

    def spawn(*a, **kw):
        mm.events.append("spawn")
        return real_spawn(*a, **kw)

    mm.host.spawn = spawn
    yield mm
    mm.close()


def test_the_start_stops_the_pages_own_policy_server_first_and_then_launches(m):
    m.snap = snap()
    answer = m.start()
    assert answer["accepted"] is True and answer["name"] == NAME
    assert m.events == ["stop", "spawn"], "the stop comes before the launch"
    assert len(m.stop_calls) == 1
    assert m.stop_calls[0].startswith("req-"), "the start's own request id"
    history = (m.sup._dir / "history.jsonl").read_text()
    assert "own_policy_stop" in history


def test_the_challenge_stops_nothing(m):
    m.snap = snap()
    m.present()
    m.sup.challenge({"purpose": "start", "name": NAME})
    assert m.stop_calls == [] and m.events == []


def test_a_machine_without_the_pages_server_stops_nothing(m):
    m.snap = snap(own=False)
    assert m.start()["accepted"] is True
    assert m.stop_calls == [] and m.events == ["spawn"]


def test_only_a_warning_triggers_the_stop_not_a_row_that_merely_names_the_unit(m):
    clear = snap(own=False)
    for r in clear["rows"]:
        if r["code"] in ("ports_free", "no_policy_deploy"):
            r["params"]["own_policy"] = UNIT  # a leftover param on a passing row
    m.snap = clear
    assert m.start()["accepted"] is True
    assert m.stop_calls == [] and m.events == ["spawn"]


def test_a_warning_without_the_pages_unit_does_not_start_and_stops_nothing(m):
    bare = snap()
    for r in bare["rows"]:
        r["params"].pop("own_policy", None)
    m.snap = bare
    m.present()
    with pytest.raises(aeri_run.Refuse) as caught:
        m.sup.challenge({"purpose": "start", "name": NAME})
    assert caught.value.code == "readiness_failed"
    assert m.stop_calls == [] and m.spawned == []


def test_a_foreign_unit_name_does_not_pass_either(m):
    foreign = snap()
    for r in foreign["rows"]:
        if "own_policy" in r["params"]:
            r["params"]["own_policy"] = "levi-aeri-policy-manual-eggplant"
    m.snap = foreign
    m.present()
    with pytest.raises(aeri_run.Refuse) as caught:
        m.sup.challenge({"purpose": "start", "name": NAME})
    assert caught.value.code == "readiness_failed" and m.stop_calls == []


@pytest.mark.parametrize(
    "answer",
    [
        RuntimeError("boom"),
        types.SimpleNamespace(),  # not even a dict
        {"state": "stopping", "stopped": False},
        {"state": "ready"},
        {},
    ],
)
def test_a_stop_that_fails_or_does_not_end_idle_means_no_launch(m, answer):
    m.snap = snap()
    m.stop_answer = answer
    status, code, _extra = refusal(m.start)
    assert (status, code) == (409, "policy_stop_failed")
    assert m.spawned == [] and m.events == ["stop"]


def test_the_stops_own_refusal_is_named_and_nothing_launches(m):
    class Http(Exception):
        pass

    err = Http()
    err.detail = {"code": "client_connected", "established": 1}
    m.snap = snap()
    m.stop_answer = err
    _status, code, extra = refusal(m.start)
    assert code == "policy_stop_failed" and extra["why"] == "client_connected"
    assert m.spawned == []


def test_rows_that_are_still_the_exception_after_the_stop_mean_no_launch(m):
    m.snap = snap()
    m.after_stop = lambda: snap(sha="snap-still")  # the unit is still there
    _status, code, extra = refusal(m.start)
    assert code == "policy_stop_failed" and extra["why"] == "still_there"
    assert m.spawned == [] and m.events == ["stop"]


def test_a_row_that_refuses_after_the_stop_is_final(m):
    m.snap = snap()
    bad = {"code": "gpu_free", "level": "refuse", "params": {}}
    m.after_stop = lambda: {
        "rows": [bad, *snap(own=False)["rows"][1:]],
        "ready": False,
        "sha256": "snap-after",
    }
    _status, code, extra = refusal(m.start)
    assert code == "readiness_failed" and extra["first"] == "gpu_free"
    assert m.spawned == [] and m.events == ["stop"]


def test_the_real_stop_goes_through_the_deploy_supervisors_own_stop(monkeypatch):
    """``aeri_run._stop_own_policy``: the deploy stop with its phrase, a request id
    of its own, and the supervisor's cache forgotten afterwards."""
    calls = []

    class Fake:
        def stop(self, body):
            calls.append(("stop", dict(body)))
            return {"state": "idle", "stopped": True}

        def invalidate(self):
            calls.append(("invalidate",))

    monkeypatch.setattr(deploy_api, "supervisor", lambda: Fake())
    got = aeri_run._stop_own_policy("req-0123456789")
    assert got["state"] == "idle"
    assert calls[0][0] == "stop" and calls[-1] == ("invalidate",)
    body = calls[0][1]
    assert body["confirm"] == "stop policy server"
    assert body["request_id"] == "aeri-req-0123456789"
    assert set(body) == {"request_id", "confirm"}, "no client override"


def test_the_real_stop_forgets_the_view_also_when_it_raises(monkeypatch):
    calls = []

    class Fake:
        def stop(self, body):
            raise RuntimeError("no")

        def invalidate(self):
            calls.append("invalidate")

    monkeypatch.setattr(deploy_api, "supervisor", lambda: Fake())
    with pytest.raises(RuntimeError):
        aeri_run._stop_own_policy("req-0123456789")
    assert calls == ["invalidate"]
