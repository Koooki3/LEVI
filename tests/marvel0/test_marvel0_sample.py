"""``src/components/marvel0/__tests__/marvel0-status.sample.json``: the committed
answer of ``GET /api/levi/marvel0/status`` that the frontend tests load. It is
what ``levi.marvel0.api.collect`` gives on a fully fake marvel0, so the server and the
page agree on one shape; ``regen_sample.py`` rewrites it."""

import json
import os

import pytest
from regen_sample import QUIET_SAMPLE, ROOT, SAMPLE, build
from test_marvel0_status import assert_schema

REGENERATE = "uv run --no-sync python tests/marvel0/regen_sample.py"


@pytest.mark.parametrize("path, busy", [(SAMPLE, True), (QUIET_SAMPLE, False)])
def test_the_committed_sample_is_what_the_server_builds(path, busy):
    name = path.relative_to(ROOT)
    assert path.is_file(), f"{name} is missing; create it with: {REGENERATE}"
    assert path.read_text() == build(busy), (
        f"{name} differs from the marvel0 status the server builds; "
        f"regenerate it with: {REGENERATE}"
    )


def test_the_quiet_sample_shows_an_idle_marvel0():
    doc = json.loads(QUIET_SAMPLE.read_text())
    assert_schema(doc)
    assert doc["marker"] == {"state": "absent", "age_s": None, "detail": None}
    assert doc["eval_sessions"]["sessions"] == [] and doc["units"]["items"] == []
    assert doc["robot_idle"] == {"state": "idle", "reasons": []}


def test_the_sample_follows_the_contract_and_shows_a_busy_marvel0():
    doc = json.loads(SAMPLE.read_text())
    assert_schema(doc)
    assert doc["sampled_at"] == "2026-10-19T15:30:00+08:00"
    assert doc["marker"] == {"state": "present", "age_s": 95.0, "detail": None}
    assert doc["eval_sessions"]["sessions"] == [
        {"group": "pi05_run_a", "age_s": 12.0, "status": "running"}
    ]
    assert [u["active"] for u in doc["units"]["items"]] == ["active"]
    assert doc["robot_idle"]["state"] == "busy"
    assert [r["source"] for r in doc["robot_idle"]["reasons"]] == [
        "marker",
        "eval_sessions",
        "units",
    ]
    assert [(r["code"], r["params"]) for r in doc["robot_idle"]["reasons"]] == [
        ("marker.present", {"age_s": 95.0}),
        (
            "sessions.recent",
            {"group": "pi05_run_a", "age_s": 12.0, "status": "running"},
        ),
        (
            "units.active",
            {
                "name": "levi-aeri-policy-pi05.service",
                "active": "active",
                "sub": "running",
            },
        ),
    ]


def test_the_build_does_not_depend_on_the_machines_time_zone(monkeypatch):
    import time

    expected = build()
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        assert build() == expected
        assert os.environ["TZ"] == "America/New_York"  # put back after the build
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize("key", ["marker", "eval_sessions", "units", "robot_idle"])
def test_the_sample_holds_no_path(key):
    assert "/" not in json.dumps(json.loads(SAMPLE.read_text())[key])
