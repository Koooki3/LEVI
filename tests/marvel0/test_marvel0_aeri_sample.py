"""``src/components/marvel0/__tests__/marvel0-aeri-run.samples.json``,
``marvel0-aeri-run.answers.json`` (and the twins in ``tests/marvel0/fixtures``):
the committed documents and answers of the hosted AERI terminal D that the
frontend tests and the other backend modules parse. They are what the backend
builds (``regen_aeri_run_sample.py``), so a field the backend renames, drops or
adds fails here, naming the command that rewrites the files, instead of leaving
the page reading a shape that no longer exists."""

import json
import re
import time

import pytest
from regen_aeri_run_sample import (
    ANSWERS,
    CODES,
    SAMPLE,
    TWIN,
    build,
    build_answers,
    build_codes,
)
from regen_sample import ROOT

from levi.marvel0 import aeri_state as st

REGENERATE = "uv run --no-sync python tests/marvel0/regen_aeri_run_sample.py"
PATH_IN_TEXT = re.compile(r"(?<![\w.:-])/(?:[A-Za-z0-9_.~+@%=,-]+/)+")


@pytest.mark.parametrize(
    ("path", "builder"),
    [
        (SAMPLE, build),
        (TWIN, build),
        (ANSWERS, build_answers),
        (CODES, build_codes),
    ],
    ids=["samples", "twin", "answers", "codes"],
)
def test_the_committed_file_is_what_the_backend_builds(path, builder):
    name = path.relative_to(ROOT)
    assert path.is_file(), f"{name} is missing; create it with: {REGENERATE}"
    assert path.read_text() == builder(), (
        f"{name} differs from what the backend builds; regenerate it with: {REGENERATE}"
    )


def test_the_build_does_not_depend_on_the_machines_time_zone(monkeypatch):
    expected = build()
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        assert build() == expected
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.fixture(name="samples")
def load():
    return json.loads(SAMPLE.read_text())["samples"]


def test_the_samples_name_every_state_the_page_reads(samples):
    assert {doc["state"]["id"] for doc in samples.values()} == set(st.STATES)


def test_every_sample_has_the_same_closed_set_of_fields(samples):
    first = set(next(iter(samples.values())))
    for name, doc in samples.items():
        assert set(doc) == first, name
        assert doc["schema"] == st.SCHEMA, name
        assert set(doc["state"]) == {
            "id",
            "reason",
            "params",
            "seq",
            "since",
            "may_move",
            "settled",
            "prompt",
        }, name
        for key in doc["keys"]:
            assert set(key) == {
                "id",
                "byte",
                "level",
                "motion",
                "slot",
                "variant",
                "phrase",
                "allowed",
                "why_not",
                "token",
                "not_before",
            }, name
        assert set(doc["stop"]) == {"allowed", "phase", "sent_at", "why_not"}, name
        assert set(doc["end"]) == {"allowed", "why_not", "phrase"}, name
        assert set(doc["start"]) == {"allowed", "why_not", "attest", "phrase"}, name
        assert set(doc["progress"]) == {
            "round",
            "rounds",
            "done",
            "released",
            "verdict_latched",
            "discarded",
            "step",
            "max_steps",
        }, name
        assert set(doc["agree"]) == {"journal", "screen", "termios"}, name


def test_enums_are_closed(samples):
    for name, doc in samples.items():
        assert doc["state"]["id"] in st.STATES, name
        assert doc["state"]["reason"] in st.REASONS, (name, doc["state"]["reason"])
        assert doc["stop"]["phase"] in st.STOP_PHASES, name
        for key in doc["keys"]:
            assert key["id"] in st.ACTIONS, name
            assert key["slot"] in st.SLOTS and key["level"] in st.LEVELS, name
            assert key["motion"] in st.MOTIONS, name
            assert key["variant"] in (None, *st.VARIANTS), name
        for notice in doc["notices"]:
            assert notice["code"] in st.NOTICES, name
        if doc["state"]["prompt"]:
            assert doc["state"]["prompt"]["kind"] in st.PROMPT_KINDS, name


def test_a_key_that_is_not_allowed_says_why_and_one_that_is_has_a_token(samples):
    for name, doc in samples.items():
        for key in doc["keys"]:
            assert (key["why_not"] is None) == key["allowed"], (name, key["id"])
            assert (key["token"] is not None) == key["allowed"], (name, key["id"])
            if key["why_not"]:
                assert key["why_not"]["code"] in st.WHY_NOT, (name, key["id"])
        for block in ("stop", "end", "start"):
            entry = doc[block]
            assert (entry["why_not"] is None) == entry["allowed"], (name, block)
            if entry["why_not"]:
                assert entry["why_not"]["code"] in st.WHY_NOT, (name, block)


def test_only_the_bytes_of_the_closed_table_appear(samples):
    allowed = {"Enter", "y", "n", "q", "o", "p", "h", "s", "f", "d", "execute"}
    for name, doc in samples.items():
        for key in doc["keys"]:
            assert key["byte"] in allowed, (name, key["byte"])
        for row in doc["history"]:
            assert row["byte"] in (*allowed, "Ctrl+C", None), (name, row)


def test_the_phrase_is_given_for_level_c_keys_only(samples):
    for name, doc in samples.items():
        for key in doc["keys"]:
            assert (key["phrase"] is not None) == (key["level"] == "C"), (
                name,
                key["id"],
            )
            if key["phrase"]:
                assert key["phrase"].startswith("execute aeri-"), name


def test_the_keys_of_each_state_are_the_ones_the_design_gives(samples):
    offered = {
        "launching": [],
        "services": [],
        "await_phrase": ["execute"],
        "await_phrase_variant": ["execute_variant", "execute"],
        "await_home_o": ["home_o", "quit"],
        "await_home_p": ["home_p", "quit"],
        "await_home_h": ["home_h", "quit"],
        "await_inventory": ["inventory_yes", "inventory_no", "quit"],
        "await_release": ["release_round", "quit"],
        "forward_running": ["verdict_success", "verdict_failure", "verdict_discard"],
        "await_resume_confirm": ["resume_confirm", "resume_cancel", "quit"],
    }
    for name, doc in samples.items():
        state = doc["state"]["id"]
        ids = [k["id"] for k in doc["keys"]]
        if state in offered:
            assert ids == offered[state], name
        elif state == "await_resume":
            assert ids == ["resume_ask", "quit"], name
        else:
            assert ids == [], (name, ids)


def test_stop_and_end_follow_the_rules(samples):
    for name, doc in samples.items():
        state = doc["state"]["id"]
        if state in ("idle", "ended", "fault_locked", "orphaned"):
            assert doc["stop"]["allowed"] is False, name
        if doc["end"]["allowed"]:
            assert state == "stalled", name
        if state == "stalled":
            assert doc["end"]["allowed"] is True, name
        if doc["stop"]["allowed"]:
            assert doc["d"]["alive"] is True and doc["stop"]["phase"] in (
                "idle",
                "unconfirmed",
            )
    assert samples["stopping_unconfirmed"]["stop"]["allowed"] is True
    assert samples["stopping"]["stop"]["allowed"] is False


def test_verdict_keys_only_in_the_forward_loop_and_only_once_per_round(samples):
    for name, doc in samples.items():
        verdicts = [k for k in doc["keys"] if k["id"].startswith("verdict_")]
        if verdicts:
            assert doc["state"]["id"] == "forward_running", name
    used = samples["forward_running_verdict_used"]
    assert used["progress"]["verdict_latched"] is True
    assert {k["why_not"]["code"] for k in used["keys"]} == {"verdict_once"}


def test_the_second_step_keys_sit_in_the_second_slot(samples):
    for name, doc in samples.items():
        for key in doc["keys"]:
            if key["id"] in (
                "resume_confirm",
                "resume_cancel",
                "home_p",
                "home_h",
            ) or key["id"].startswith("verdict_"):
                assert key["slot"] == "second", (name, key["id"])
            if key["id"] in (
                "release_round",
                "inventory_yes",
                "home_o",
                "execute",
                "resume_ask",
            ):
                assert key["slot"] == "first", (name, key["id"])


def test_the_two_enters_differ_in_what_they_say(samples):
    confirm = next(
        k for k in samples["await_resume"]["keys"] if k["id"] == "resume_ask"
    )
    direct = next(
        k for k in samples["await_resume_direct"]["keys"] if k["id"] == "resume_ask"
    )
    assert (confirm["variant"], confirm["level"], confirm["motion"]) == (
        "confirm_reason",
        "A",
        "none",
    )
    assert (direct["variant"], direct["level"], direct["motion"]) == (
        "direct",
        "B",
        "may_move",
    )
    assert samples["await_resume"]["wait"]["confirm"] is True
    assert samples["await_resume_direct"]["wait"]["restore_scene"] is True


def test_no_sample_names_a_path_or_a_judgement(samples):
    text = json.dumps(samples)
    assert "/home/" not in text and not PATH_IN_TEXT.search(text.replace("://", ""))
    for word in ("subtask_check", "subtask_plan", "subtask_retry", "raw_content"):
        assert word not in text


def test_the_answers_and_the_refusals_are_complete():
    data = json.loads(ANSWERS.read_text())
    assert set(data["answers"]) == {"challenge", "start", "key", "stop", "log"}
    assert set(data["refusals"]) == set(st.REFUSALS)
    for code, answer in data["refusals"].items():
        assert answer["detail"]["code"] == code
        assert answer["status"] == st.REFUSALS[code][0]
        assert answer["detail"]["message"]
    assert data["answers"]["start"]["state"] == "launching"
    assert data["answers"]["key"]["evidence"] == "pending"
    assert data["answers"]["stop"]["via"] == "pty"
    for answer in data["answers"].values():
        assert not PATH_IN_TEXT.search(json.dumps(answer))


def test_the_codes_file_lists_every_code_of_the_module():
    codes = json.loads(CODES.read_text())
    assert codes["states"] == list(st.STATES)
    assert set(codes["errors"]) == set(st.REFUSALS)
    assert set(st.WHY_NOT) <= set(st.REFUSALS), (
        "every reason a key is refused is a route error too"
    )
