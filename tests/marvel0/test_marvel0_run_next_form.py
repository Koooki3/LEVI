"""Next-episode drafts expose only the original client form, not launch internals."""

from types import SimpleNamespace

from levi.marvel0.run import _run_view


def test_next_episode_form_is_allowlisted_and_does_not_change_record():
    record = {
        "run_id": "old",
        "prompt": "old task",
        "created_epoch": 1,
        "form": {
            "prompt": "old task",
            "rtc": True,
            "min_z": 0.1,
            "argv": ["private"],
            "environment": {"secret": "private"},
        },
    }
    view = _run_view(SimpleNamespace(record=record, now=2))
    assert view["run_id"] == "old"
    assert view["form"] == {"prompt": "old task", "rtc": True, "min_z": 0.1}
    view["form"]["prompt"] = "new task"
    assert record["form"]["prompt"] == "old task"


def test_legacy_record_does_not_invent_a_next_episode_form():
    assert _run_view(SimpleNamespace(record={"run_id": "old"}, now=2))["form"] is None


def test_production_manual_switch_is_explicit_and_fail_closed(monkeypatch):
    from levi.marvel0 import control_site, run

    assert run.Host().manual_enabled() is False
    monkeypatch.setattr(control_site, "load", lambda: None)
    assert run.production_host().manual_enabled() is False
    monkeypatch.setattr(
        control_site,
        "load",
        lambda: SimpleNamespace(manual=SimpleNamespace(enabled=True), manual_errors=()),
    )
    assert run.production_host().manual_enabled() is True
    monkeypatch.setattr(
        control_site,
        "load",
        lambda: SimpleNamespace(
            manual=SimpleNamespace(enabled=True),
            manual_errors=(("enabled", "invalid"),),
        ),
    )
    assert run.production_host().manual_enabled() is False


def test_the_run_view_carries_the_acceleration_rule_and_an_old_record_ran_v1():
    v2 = {"run_id": "new", "created_epoch": 1, "chunk_accel": True}
    view = _run_view(SimpleNamespace(record={**v2, "chunk_accel_rule": "v2"}, now=2))
    assert (view["chunk_accel"], view["chunk_accel_rule"]) == (True, "v2")
    # a record from before the rule, or one holding anything else, is v1 (the client's default)
    for rule in ({}, {"chunk_accel_rule": None}, {"chunk_accel_rule": "v4"}):
        view = _run_view(SimpleNamespace(record={**v2, **rule}, now=2))
        assert view["chunk_accel_rule"] == "v1", rule
        assert (view["chunk_accel_lambda"], view["chunk_accel_alpha"]) == (None, None)
    assert (
        _run_view(SimpleNamespace(record={"run_id": "old"}, now=2))["chunk_accel_rule"]
        == "v1"
    )


def test_the_run_view_carries_the_v3_numbers_only_when_they_are_numbers():
    rec = {"run_id": "new", "created_epoch": 1, "chunk_accel": True}
    v3 = {**rec, "chunk_accel_rule": "v3"}
    view = _run_view(
        SimpleNamespace(
            record={**v3, "chunk_accel_lambda": 2.5, "chunk_accel_alpha": 0.01}, now=2
        )
    )
    assert view["chunk_accel_rule"] == "v3"
    assert (view["chunk_accel_lambda"], view["chunk_accel_alpha"]) == (2.5, 0.01)
    # an integer is a number, anything else is no answer
    view = _run_view(
        SimpleNamespace(
            record={**v3, "chunk_accel_lambda": 2, "chunk_accel_alpha": None}, now=2
        )
    )
    assert (view["chunk_accel_lambda"], view["chunk_accel_alpha"]) == (2.0, None)
    for bad in (True, "2.0", float("nan"), float("inf"), [2.0]):
        view = _run_view(
            SimpleNamespace(
                record={**v3, "chunk_accel_lambda": bad, "chunk_accel_alpha": bad},
                now=2,
            )
        )
        assert (view["chunk_accel_lambda"], view["chunk_accel_alpha"]) == (
            None,
            None,
        ), bad
    # the form of the run goes back with its numbers, so the next episode keeps them
    form = {
        "model_id": "m",
        "prompt": "p",
        "chunk_accel": True,
        "chunk_accel_rule": "v3",
        "chunk_accel_lambda": 2.5,
        "chunk_accel_alpha": 0.01,
        "not_a_field": 1,
    }
    view = _run_view(SimpleNamespace(record={**v3, "form": form}, now=2))
    assert view["form"]["chunk_accel_lambda"] == 2.5
    assert view["form"]["chunk_accel_alpha"] == 0.01
    assert "not_a_field" not in view["form"]
