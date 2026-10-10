"""The run header the journal writes (T-JNL-1): ``reset_mode`` and
``scene_check`` when the caller gives them, every line at the run event's
own minor, the per-contract minors in ``contracts``; an old (minor-0) log
still opens, takes appends at its own minor and recovers."""

import json
from pathlib import Path

import pytest

from levi.automatic import journal as J
from levi.domain import aeri

HERE = Path(__file__).resolve().parent
OLD = HERE / "journals" / "minor0" / J.JOURNAL
RUN = "r-head"
PLAN = "ab" * 32


def who(kind="orchestrator"):
    return {
        "principal_kind": kind,
        "principal_id": f"{kind}-1",
        "session_id": "s-1",
        "process": J.process_identity(),
        "command_id": None,
    }


def new(directory, **kwargs):
    return J.Journal.create(
        directory, run_id=RUN, plan_sha256=PLAN, authority=who(), **kwargs
    )


def raw_lines(directory):
    return [
        json.loads(line)
        for line in (Path(directory) / J.JOURNAL).read_bytes().splitlines()
    ]


# --- step 1: what the header and every line carry ----------------------------------------


def test_every_line_is_written_at_the_run_event_minor(tmp_path):
    with new(tmp_path) as journal:
        journal.note("hello", authority=who())
    minors = {line["minor"] for line in raw_lines(tmp_path)}
    assert minors == {aeri.MINORS["run_event"]}


def test_contracts_name_each_contract_at_its_own_minor(tmp_path):
    new(tmp_path).close()
    header = raw_lines(tmp_path)[0]["header"]
    assert {c["schema"]: c["minor"] for c in header["contracts"]} == {
        aeri.SCHEMAS[name]: aeri.MINORS[name] for name in aeri.SCHEMAS
    }


def test_without_modes_the_header_leaves_them_unset(tmp_path):
    new(tmp_path).close()
    header = J.Journal.read(tmp_path).events[0].header
    assert (header.reset_mode, header.scene_check) == (None, None)


def test_the_header_names_the_modes_it_is_given(tmp_path):
    new(tmp_path, reset_mode="human_assisted", scene_check="operator_attested").close()
    found = J.Journal.read(tmp_path)
    assert found.corrupt is None
    header = found.events[0].header
    assert (header.reset_mode, header.scene_check) == (
        "human_assisted",
        "operator_attested",
    )
    assert aeri.header_modes(header) == {
        "reset_mode": "human_assisted",
        "scene_check": "operator_attested",
    }


def test_one_mode_alone_is_written_alone(tmp_path):
    new(tmp_path, reset_mode="single_reset_policy").close()
    header = J.Journal.read(tmp_path).events[0].header
    assert (header.reset_mode, header.scene_check) == ("single_reset_policy", None)


@pytest.mark.parametrize(
    "modes",
    [{"reset_mode": "single_policy"}, {"scene_check": "human"}],
)
def test_an_alias_is_refused_and_leaves_no_header_and_no_writer(tmp_path, modes):
    with pytest.raises(J.JournalRefused) as caught:
        new(tmp_path, **modes)
    assert caught.value.code == "E_CONTRACT"
    assert J.Journal.read(tmp_path).events == []
    new(tmp_path).close()  # the lock was released; the run can start again
