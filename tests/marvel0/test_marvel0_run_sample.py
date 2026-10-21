"""``src/components/marvel0/__tests__/marvel0-run.samples.json`` and
``marvel0-run.answers.json``: the committed documents and answers of the hosted
run that the frontend tests parse with the page's own readers. They are what the
backend builds (``regen_run_sample.py``), so a field the backend renames, drops
or adds fails here, naming the command that rewrites the files, instead of
leaving the page reading a shape that no longer exists."""

import json
import re
import tempfile
import time

import pytest
from regen_deploy_sample import _lines  # noqa: F401 - (the printer the files use)
from regen_run_sample import ANSWERS, SAMPLE, build, build_answers
from regen_sample import ROOT

from levi.marvel0 import run

REGENERATE = "uv run --no-sync python tests/marvel0/regen_run_sample.py"
PATH_IN_TEXT = re.compile(r"(?<![\w.:-])/(?:[A-Za-z0-9_.~+@%=,-]+/)+")


@pytest.mark.parametrize(
    "path, builder",
    [(SAMPLE, build), (ANSWERS, build_answers)],
    ids=["samples", "answers"],
)
def test_the_committed_file_is_what_the_backend_builds(path, builder):
    name = path.relative_to(ROOT)
    assert path.is_file(), f"{name} is missing; create it with: {REGENERATE}"
    assert path.read_text() == builder(), (
        f"{name} differs from what the backend builds; regenerate it with: {REGENERATE}"
    )


def test_the_build_does_not_depend_on_the_machines_time_zone(monkeypatch):
    expected = (build(), build_answers())
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        assert (build(), build_answers()) == expected
    finally:
        monkeypatch.undo()
        time.tzset()


def test_the_build_does_not_depend_on_the_checkouts_path(monkeypatch, tmp_path):
    expected = build_answers()
    long_tmp = tmp_path / ("x" * 37) / "deeper"
    long_tmp.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(long_tmp))
    monkeypatch.setattr(tempfile, "tempdir", None)  # read TMPDIR again
    assert build_answers() == expected


def test_the_samples_name_every_state_the_page_reads():
    samples = json.loads(SAMPLE.read_text())
    assert {doc["state"] for doc in samples.values()} == set(run.STATES)
    by_name = {name: doc["state"] for name, doc in samples.items()}
    assert (
        by_name["idle_ready"] == "idle" and samples["idle_ready"]["can_start"] is True
    )
    assert samples["preflight_failing"]["can_start"] is False
    assert {r["level"] for r in samples["preflight_failing"]["preflight"]} >= {
        "refuse",
        "unknown",
        "warn",
    }
    for name, doc in samples.items():
        assert doc["schema"] == "levi.marvel0.run.v1", name
        assert set(doc["actions"]) == set(run.ACTIONS), name
        for entry in doc["actions"].values():
            assert (entry["why_not"] is None) == entry["allowed"], name
        assert doc["notices"][0]["code"] == "gates_scope", name


def test_the_samples_never_offer_s_p_i_or_a_digit_at_the_start_gate():
    samples = json.loads(SAMPLE.read_text())
    for name, doc in samples.items():
        if doc["state"] != "at_gate":
            continue
        allowed = {a for a, e in doc["actions"].items() if e["allowed"]}
        assert allowed <= {"enter", "end_at_gate", "hold"}, name
        assert doc["keys"]["enter"] == "Enter" and doc["keys"]["end_at_gate"] == "q"
    assert set(samples["at_gate"]["keys"].values()) == {"Enter", "s", "f", "d", "q"}


def test_the_answers_name_the_refusals_the_page_words():
    answers = json.loads(ANSWERS.read_text())
    assert set(answers["refusals"]) >= {
        "confirm_required",
        "attest_required",
        "challenge_invalid",
        "challenge_expired",
        "preflight_failed",
        "min_z_below_task_floor",
        "run_disabled",
        "not_product",
        "state_not_allowed",
        "key_locked",
        "enter_locked",
        "monitor_red",
        "monitor_not_green",
        "busy",
    }
    for name, row in answers["refusals"].items():
        assert set(row) == {"status", "detail"}, name
        assert row["detail"]["code"] and row["detail"]["message"], name
    assert answers["refusals"]["preflight_failed"]["detail"]["checks"]
    assert answers["answers"]["started"]["created"] is True
    assert answers["answers"]["started_again"]["replayed"] is True
    assert answers["answers"]["hold_again"]["already"] is True


def test_the_samples_hold_no_path_but_the_templates():
    for path in (SAMPLE, ANSWERS):
        text = json.dumps(json.loads(path.read_text()))
        found = set(PATH_IN_TEXT.findall(text))
        assert all(p.startswith("/path/to/") for p in found), (path.name, found)
        assert "/home/" not in text and ".state" not in text
