"""``src/components/marvel0/__tests__/marvel0-aeri.samples.json`` and
``tests/marvel0/fixtures/aeri_ready.sample.json``: the committed documents and
answers of the AERI preparation card that the frontend tests parse with the
page's own readers. They are what the backend builds
(``regen_aeri_ready_sample.py``), so a field the backend renames, drops or adds
fails here, naming the command that rewrites the files, instead of leaving the
page reading a shape that no longer exists."""

import json
import re
import time

import pytest
from regen_aeri_ready_sample import (
    FIXTURE,
    REGENERATE,
    ROOT,
    SAMPLE,
    STACK_UP,
    WHY_NOT,
    build_all,
)

from levi.marvel0 import aeri_prep, aeri_ready

SOURCE = SAMPLE
PATH_IN_TEXT = re.compile(r"(?<![\w.:-])/(?:[A-Za-z0-9_.~+@%=,-]+/)+")


@pytest.fixture(scope="module")
def built_both():
    return build_all()


@pytest.fixture(scope="module")
def built(built_both):
    return built_both[0]


def build():
    return build_all()[0]


@pytest.fixture(scope="module")
def sample(built):
    return json.loads(built)


@pytest.mark.parametrize("path", [SAMPLE, FIXTURE], ids=["frontend", "fixture"])
def test_the_committed_file_is_what_the_backend_builds(path, built):
    name = path.relative_to(ROOT)
    assert path.is_file(), f"{name} is missing; create it with: {REGENERATE}"
    assert path.read_text() == built, (
        f"{name} differs from what the backend builds; regenerate it with: {REGENERATE}"
    )


def test_the_stack_up_rows_are_what_the_backend_builds(built_both):
    name = STACK_UP.relative_to(ROOT)
    assert STACK_UP.name == "aeri_stack_up_rows.json"
    assert STACK_UP.is_file(), f"{name} is missing; create it with: {REGENERATE}"
    assert STACK_UP.read_text() == built_both[1], (
        f"{name} differs from what the backend builds; regenerate it with: {REGENERATE}"
    )


def test_the_stack_up_rows_hold_the_rule_of_the_start_gate(built_both):
    """What the gate may rely on: ``ready`` and ``warn`` only for ``warn_ok``."""
    data = json.loads(built_both[1])
    assert data["warn_ok"] == list(aeri_ready.WARN_OK)
    up = data["snapshots"]["stack_up"]
    assert up["ready"] is True
    assert [r["code"] for r in up["rows"]] == list(aeri_ready.ROW_CODES)
    below = {r["code"]: r["level"] for r in up["rows"] if r["level"] != "ok"}
    assert below == {"stack_ready": "warn"}, "the one row that can only be warn"
    for name in ("stack_not_read", "stack_down"):
        assert data["snapshots"][name]["ready"] is False, name
    for snap in data["snapshots"].values():
        warned = {r["code"] for r in snap["rows"] if r["level"] == "warn"}
        assert warned <= set(data["warn_ok"])


def test_the_build_does_not_depend_on_the_time_zone(built, monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        assert build() == built
    finally:
        monkeypatch.undo()
        time.tzset()


def test_the_build_does_not_depend_on_where_the_checkout_keeps_its_temporary_files(
    built, monkeypatch, tmp_path
):
    import regen_aeri_ready_sample as regen

    deeper = tmp_path / ("x" * 41) / "deeper"
    deeper.mkdir(parents=True)
    monkeypatch.setattr(regen, "_tmp_parent", lambda: deeper)
    assert build() == built


def test_every_state_has_the_same_closed_document(sample):
    docs = sample["get"]
    keys = {frozenset(d) for d in docs.values()}
    assert len(keys) == 1, "every state is one document with the same keys"
    for name, doc in docs.items():
        assert doc["schema"] == "levi.marvel0.aeri.v1", name
        assert [r["code"] for r in doc["readiness"]] in (
            [],
            ["no_e_run"],  # (a light read while the page's own client is on)
            list(aeri_ready.ROW_CODES),
        ), name
        for row in doc["readiness"]:
            assert set(row) == {"code", "level", "text", "params"}
            assert row["level"] in sample["codes"]["levels"] and row["text"]


def test_the_states_cover_what_the_card_shows(sample):
    docs = sample["get"]
    assert set(docs) == {
        "unconfigured",
        "invalid_section",
        "disabled",
        "busy",
        "blocked",
        "not_prepared",
        "prepared_ready",
        "prepared_unit_short",
        "prepared_changed",
        "leftovers_present",
        "leftovers_blocked",
    }
    levels = {r["level"] for d in docs.values() for r in d["readiness"]}
    assert levels == {"ok", "warn", "refuse", "unknown"}, "every level is shown once"
    assert docs["unconfigured"]["site"]["state"] == "unconfigured"
    assert docs["unconfigured"]["site"]["template"]
    assert docs["invalid_section"]["site"]["state"] == "invalid"
    assert docs["invalid_section"]["site"]["errors"]
    assert docs["disabled"]["enabled"] is False and docs["disabled"]["readiness"] == []
    assert docs["disabled"]["live_service"] is None
    assert docs["busy"]["enabled"] is True and docs["busy"]["ready"] is False
    assert [r["code"] for r in docs["busy"]["readiness"]] == ["no_e_run"]
    assert docs["busy"]["live_service"] is None
    assert docs["prepared_ready"]["ready"] is True
    # the product's service unit stops after 15 s: a warning row, the card still ready
    short = docs["prepared_unit_short"]
    unit = next(r for r in short["readiness"] if r["code"] == "unit_timeout")
    assert short["ready"] is True and unit["level"] == "warn"
    assert unit["params"] == {"seconds": 15.0, "need_s": 150}
    ok = next(
        r for r in docs["prepared_ready"]["readiness"] if r["code"] == "unit_timeout"
    )
    assert ok["level"] == "ok"
    assert docs["prepared_changed"]["prepared"]["problems"] == ["files_changed"]
    assert docs["blocked"]["live_service"]["state"] == "running"
    assert docs["blocked"]["live_service"]["can_stop"] is True
    assert docs["leftovers_present"]["leftovers"]["can_stop"] is True
    assert docs["leftovers_blocked"]["leftovers"]["why_not"]["code"] == "gpu_in_use"


def test_every_code_the_page_must_word_is_listed_and_has_a_sample(sample):
    codes = sample["codes"]
    assert codes["readiness"] == list(aeri_ready.ROW_CODES)
    assert codes["errors"] == sorted(aeri_prep.REFUSALS)
    shown = set()
    for key in ("prepare", "live_stop", "leftovers_stop"):
        shown |= set(sample[key]["errors"])
    assert shown == set(codes["errors"]) - {"confirm_required"} | {
        "confirm_required"
    }, set(codes["errors"]) ^ shown
    for key in ("prepare", "live_stop", "leftovers_stop"):
        for code, answer in sample[key]["errors"].items():
            assert answer["detail"]["code"] == code
            assert answer["status"] == aeri_prep.REFUSALS[code][0]
            assert answer["detail"]["message"]
    why = {
        d[block]["why_not"]["code"]
        for d in sample["get"].values()
        for block in ("live_service", "leftovers")
        if d[block] and d[block]["why_not"]
    }
    assert why <= set(WHY_NOT), why - set(WHY_NOT)
    assert set(sample["constructed"]) <= set(codes["errors"])


def test_the_samples_hold_no_path_but_the_command_and_no_secret(sample):
    def strip(value):
        if isinstance(value, dict):
            # the command and the site template are the two places with paths
            return {
                k: ("" if k in ("execute", "template") else strip(v))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    text = json.dumps(strip(sample))
    assert "/home/" not in text and "/tmp" not in text and "/path/to" not in text
    found = PATH_IN_TEXT.findall(text)
    assert found == [], found
    for word in ("token", "TOKEN", "password", "secret"):
        assert word not in text, word


def test_the_command_is_the_only_place_with_paths_and_they_are_placeholders(sample):
    prepared = sample["get"]["prepared_ready"]["prepared"]
    assert prepared["execute"].startswith("cd /path/to/openpi && ")
    assert "/home/" not in prepared["execute"]
    assert sample["prepare"]["ok"]["body"]["prepared"]["execute"] == prepared["execute"]


def test_a_prepared_run_in_the_samples_is_the_one_the_hosted_run_reads(sample):
    prepared = sample["get"]["prepared_ready"]["prepared"]
    assert set(prepared) == {
        "name",
        "mode",
        "task",
        "rounds",
        "steps",
        "gripper_confirm_s",
        "start_home",
        "profile",
        "aeri_head",
        "prepared_at",
        "plan_sha256",
        "binding_sha256",
        "files_sha256",
        "intact",
        "problems",
        "execute",
        "confirm",
    }
    assert re.fullmatch(r"aeri-\d{8}-(ext|loc)-r\d{2,}", prepared["name"])
    assert prepared["confirm"] == f"execute {prepared['name']}"
