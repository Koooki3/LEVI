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
