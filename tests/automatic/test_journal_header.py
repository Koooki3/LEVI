"""The run header the journal writes (T-JNL-1): ``reset_mode`` and
``scene_check`` when the caller gives them, every line at the run event's
own minor, the per-contract minors in ``contracts``; an old (minor-0) log
still opens, takes appends at its own minor and recovers."""

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from test_aeri_cli import CONTRACT, job_text

from levi.automatic import cli
from levi.automatic import journal as J
from levi.domain import aeri

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CHILD = HERE / "journal_header_child.py"
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


# --- step 2: reopening checks the header; an old log keeps its minor -------------------

MODES = {"reset_mode": "single_reset_policy", "scene_check": "provider"}


def reopen(directory, **kwargs):
    return J.Journal.open(directory, plan_sha256=PLAN, **kwargs)


def test_reopening_with_the_same_modes_works(tmp_path):
    new(tmp_path, **MODES).close()
    with reopen(tmp_path, **MODES, authority=who()) as journal:
        journal.note("again", authority=who())
    assert J.Journal.read(tmp_path).corrupt is None


@pytest.mark.parametrize(
    "changed",
    [
        {"reset_mode": "human_assisted"},
        {"scene_check": "operator_attested"},
        {"reset_mode": "human_assisted", "scene_check": "operator_attested"},
    ],
)
def test_reopening_with_other_modes_is_refused_with_a_note(tmp_path, changed):
    new(tmp_path, **MODES).close()
    before = (tmp_path / J.JOURNAL).read_bytes().splitlines()[0]
    with pytest.raises(J.JournalRefused) as caught:
        reopen(tmp_path, **{**MODES, **changed}, authority=who("recovery"))
    assert caught.value.code == "E_PLAN"
    for name in changed:
        assert name in caught.value.detail
    found = J.Journal.read(tmp_path)
    assert found.corrupt is None
    # The header is never rewritten; the refusal is on record.
    assert (tmp_path / J.JOURNAL).read_bytes().splitlines()[0] == before
    last = found.events[-1]
    assert last.record == "note" and last.note.code == "run_header_mismatch"
    for name in changed:
        assert name in last.note.detail
    # No writer is left behind: the right configuration opens it.
    with reopen(tmp_path, **MODES) as journal:
        assert journal.state == "PREFLIGHT"


def test_a_refusal_without_an_authority_writes_nothing(tmp_path):
    new(tmp_path, **MODES).close()
    before = (tmp_path / J.JOURNAL).read_bytes()
    with pytest.raises(J.JournalRefused):
        reopen(tmp_path, reset_mode="human_assisted")
    assert (tmp_path / J.JOURNAL).read_bytes() == before


def test_an_old_caller_that_names_no_mode_still_opens_a_new_header(tmp_path):
    new(tmp_path, **MODES).close()
    reopen(tmp_path).close()


def test_the_mode_check_runs_after_the_plan_check(tmp_path):
    new(tmp_path, **MODES).close()
    before = (tmp_path / J.JOURNAL).read_bytes()
    with pytest.raises(J.JournalRefused) as caught:
        J.Journal.open(
            tmp_path,
            plan_sha256="cd" * 32,
            reset_mode="human_assisted",
            authority=who(),
        )
    assert caught.value.code == "E_PLAN" and "plan changed" in caught.value.detail
    assert (tmp_path / J.JOURNAL).read_bytes() == before


def test_a_busy_journal_stays_busy_whatever_the_modes(tmp_path):
    held = new(tmp_path, **MODES)
    try:
        with pytest.raises(J.JournalBusy):
            reopen(tmp_path, reset_mode="human_assisted", authority=who())
        with pytest.raises(J.JournalBusy):
            reopen(tmp_path, **MODES)
    finally:
        held.close()
    assert J.Journal.read(tmp_path).events[-1].record == "run_header"


def _old_log(directory, *, without_fields=False):
    """The minor-0 fixture written by main's writer (c8c9836); with
    ``without_fields`` as a writer from before run_event minor 1 wrote it
    (no ``reset_mode``/``scene_check`` keys at all), chain recomputed."""
    directory.mkdir(parents=True, exist_ok=True)
    if not without_fields:
        shutil.copyfile(OLD, directory / J.JOURNAL)
        return directory
    previous, out = J.ZEROS, []
    for line in OLD.read_bytes().splitlines():
        value = json.loads(line)
        if value["header"] is not None:
            del value["header"]["reset_mode"], value["header"]["scene_check"]
        value["prev_sha256"] = previous
        raw = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        out.append(raw)
        previous = J.line_sha(raw)
    (directory / J.JOURNAL).write_bytes(b"\n".join(out) + b"\n")
    return directory


@pytest.mark.parametrize("without_fields", [False, True])
def test_an_old_minor_0_log_opens_appends_and_recovers_at_minor_0(
    tmp_path, without_fields
):
    run = _old_log(tmp_path / "run", without_fields=without_fields)
    scan = J.Journal.read(run)
    before = (run / J.JOURNAL).read_bytes()
    assert scan.corrupt is None and {e.minor for e in scan.events} == {0}
    header = scan.events[0].header
    assert (header.reset_mode, header.scene_check) == (None, None)
    # Its header has no modes: a configuration that names any still opens it.
    with reopen(
        run,
        reset_mode="human_assisted",
        scene_check="operator_attested",
        authority=who("recovery"),
    ) as journal:
        assert journal.minor == 0
        assert journal.open_transaction is not None  # cut short after prepared
        journal.note("reopened", authority=who())
        found = journal.recover(authority=who("recovery"))
        assert (found.state, found.reason) == ("FAULT_LOCKED", "recovery_ambiguous")
        assert found.dangling == "r-old:tx4"
    after = J.Journal.read(run)
    assert after.corrupt is None and after.effective_state == "FAULT_LOCKED"
    # One log, one minor: every appended line kept the old one.
    assert {e.minor for e in after.events} == {0}
    assert len(after.events) > len(scan.events)
    # Every old line is kept byte for byte (the header too, still without
    # modes); the new lines only follow them.
    assert (run / J.JOURNAL).read_bytes().startswith(before)
    assert after.events[0].header.reset_mode is None
    # A second restart reads and appends it again.
    with reopen(run) as journal:
        journal.recover(authority=who("recovery"))
    assert {e.minor for e in J.Journal.read(run).events} == {0}


def test_the_fixture_is_a_minor_0_log_of_the_old_writer():
    lines = OLD.read_bytes().splitlines()
    assert len(lines) == 5
    assert {json.loads(line)["minor"] for line in lines} == {0}
    header = json.loads(lines[0])["header"]
    assert {c["minor"] for c in header["contracts"]} == {0}


def test_a_new_log_never_mixes_minors_after_reopening(tmp_path):
    new(tmp_path, **MODES).close()
    with reopen(tmp_path, **MODES) as journal:
        journal.note("x", authority=who())
        journal.recover(authority=who("recovery"))
    assert {e.minor for e in J.Journal.read(tmp_path).events} == {
        aeri.MINORS["run_event"]
    }


# --- killed while the header is written -------------------------------------------------


def _child(*args):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [env.get("PYTHONPATH"), str(ROOT)])
    )
    return subprocess.run(
        [sys.executable, str(CHILD), *map(str, args)], env=env, timeout=60, check=False
    )


CHILD_MODES = {"reset_mode": "human_assisted", "scene_check": "provider"}
# The child's plan (journal_header_child.PLAN_BODY): its plan.json is on
# disk from the step before the journal file on.
CHILD_PLAN = J.plan_digest(
    {"reset_mode": "human_assisted", "scene_check": "provider", "n": 1}
)


@pytest.mark.parametrize("point", ["after_lock", "empty_file", "torn_header"])
def test_killed_before_the_header_is_whole_the_run_starts_again(tmp_path, point):
    run = tmp_path / "run"
    assert _child(run, point).returncode == -signal.SIGKILL
    found = J.Journal.read(run)
    # No run started: nothing to recover, nothing corrupt.
    assert found.events == [] and found.corrupt is None
    assert bool(found.torn) == (point == "torn_header")
    with pytest.raises(J.JournalRefused) as caught:
        J.Journal.open(run, plan_sha256=CHILD_PLAN, **CHILD_MODES)
    assert caught.value.code == "E_EMPTY"
    # The kernel released the dead writer's lock; the run is created again.
    J.Journal.create(
        run,
        run_id="r-header-crash",
        plan_sha256=CHILD_PLAN,
        authority=who(),
        **CHILD_MODES,
    ).close()
    again = J.Journal.read(run)
    assert again.corrupt is None and again.torn == b"" and len(again.events) == 1
    header = again.events[0].header
    assert (header.reset_mode, header.scene_check) == (
        "human_assisted",
        "provider",
    )
    if point == "torn_header":
        assert len(list((run / J.TORN).iterdir())) == 1  # kept for inspection


def test_killed_after_the_header_the_run_exists_and_recovers(tmp_path):
    run = tmp_path / "run"
    assert _child(run, "after_header").returncode == -signal.SIGKILL
    with pytest.raises(J.JournalRefused) as caught:
        new(run, **CHILD_MODES)
    assert caught.value.code == "E_EXISTS"
    with J.Journal.open(run, plan_sha256=CHILD_PLAN, **CHILD_MODES) as journal:
        found = journal.recover(authority=who("recovery"))
    assert found.state == "FAULT_LOCKED"
    after = J.Journal.read(run)
    assert after.corrupt is None
    assert {e.minor for e in after.events} == {aeri.MINORS["run_event"]}


# --- step 3: the plan in the run folder ---------------------------------------------------

PLAN_BODY = {
    "schema_version": "levi.aeri.job.v1",
    "run": {"episodes": 2, "folders": ("forward", "reset")},
    "reset_mode": "single_reset_policy",
    "scene_check": "provider",
    "where": Path("/somewhere"),  # not JSON: written as text, like the digest
}
BODY_SHA = J.plan_digest(PLAN_BODY)


def planned(directory, **kwargs):
    return J.Journal.create(
        directory,
        run_id=RUN,
        plan_sha256=BODY_SHA,
        authority=who(),
        plan=PLAN_BODY,
        **MODES,
        **kwargs,
    )


def test_the_plan_is_kept_next_to_the_journal(tmp_path):
    planned(tmp_path).close()
    kept = J.read_plan(tmp_path)
    assert kept["plan_sha256"] == BODY_SHA
    assert kept["plan"]["where"] == "/somewhere"
    assert J.plan_digest(kept["plan"]) == BODY_SHA
    assert J.Journal.read(tmp_path).events[0].header.plan_sha256 == BODY_SHA
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize("strategy", ["single_reset_policy", "human_assisted"])
def test_the_job_loader_and_the_journal_digest_a_plan_alike(tmp_path, strategy):
    (tmp_path / "rollouts").mkdir()
    (tmp_path / "initial-state.yaml").write_text(CONTRACT)
    job = tmp_path / "automatic-eval.yaml"
    job.write_text(job_text(tmp_path / "rollouts", strategy=strategy))
    loaded = cli.load_job(job)
    assert J.plan_digest(loaded["plan"]) == loaded["plan_sha256"]


def test_no_plan_no_file(tmp_path):
    new(tmp_path).close()
    assert J.read_plan(tmp_path) is None
    assert not (tmp_path / J.PLAN_FILE).exists()


def test_a_plan_that_does_not_hash_to_the_plan_sha256_is_refused(tmp_path):
    with pytest.raises(J.JournalRefused) as caught:
        J.Journal.create(
            tmp_path, run_id=RUN, plan_sha256=PLAN, authority=who(), plan=PLAN_BODY
        )
    assert caught.value.code == "E_PLAN"
    assert not (tmp_path / J.PLAN_FILE).exists()
    assert not (tmp_path / J.JOURNAL).exists()


def test_a_plan_kept_by_a_create_cut_short_is_accepted_again(tmp_path):
    J.keep_plan(tmp_path, PLAN_BODY, BODY_SHA)  # then the process died
    planned(tmp_path).close()
    assert J.read_plan(tmp_path)["plan_sha256"] == BODY_SHA


def test_another_runs_plan_in_the_folder_refuses_the_create(tmp_path):
    other = {**PLAN_BODY, "seed": 7}
    J.keep_plan(tmp_path, other, J.plan_digest(other))
    for call in (lambda: planned(tmp_path), lambda: new(tmp_path)):
        with pytest.raises(J.JournalRefused) as caught:
            call()
        assert caught.value.code == "E_PLAN"
    assert J.read_plan(tmp_path)["plan"]["seed"] == 7
    assert J.Journal.read(tmp_path).events == []


@pytest.mark.parametrize(
    "damage",
    [
        b"not json",
        b"[]",
        b'{"plan_sha256": "ab", "plan": {}}',
        b'{"plan_sha256": "' + BODY_SHA.encode() + b'", "plan": {"edited": 1}}',
    ],
)
def test_a_damaged_plan_file_refuses_create_and_open(tmp_path, damage):
    planned(tmp_path / "run").close()
    (tmp_path / "run" / J.PLAN_FILE).write_bytes(damage)
    with pytest.raises(J.JournalRefused) as caught:
        J.read_plan(tmp_path / "run")
    assert caught.value.code == "E_PLAN"
    with pytest.raises(J.JournalRefused) as caught:
        J.Journal.open(tmp_path / "run", plan_sha256=BODY_SHA)
    assert caught.value.code == "E_PLAN"
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / J.PLAN_FILE).write_bytes(damage)
    with pytest.raises(J.JournalRefused) as caught:
        planned(fresh)
    assert caught.value.code == "E_PLAN"


def test_reopening_with_the_plan_checks_it_and_keeps_it_for_an_old_run(tmp_path):
    J.Journal.create(
        tmp_path, run_id=RUN, plan_sha256=BODY_SHA, authority=who(), **MODES
    ).close()
    assert J.read_plan(tmp_path) is None  # a run started without its plan
    with pytest.raises(J.JournalRefused) as caught:
        J.Journal.open(tmp_path, plan_sha256=BODY_SHA, plan={**PLAN_BODY, "seed": 1})
    assert caught.value.code == "E_PLAN"
    assert J.read_plan(tmp_path) is None
    J.Journal.open(tmp_path, plan_sha256=BODY_SHA, plan=PLAN_BODY).close()
    assert J.read_plan(tmp_path)["plan_sha256"] == BODY_SHA
    J.Journal.open(tmp_path, plan_sha256=BODY_SHA, plan=PLAN_BODY).close()


def test_a_plan_file_of_another_plan_than_the_header_refuses_the_open(tmp_path):
    planned(tmp_path).close()
    other = {**PLAN_BODY, "seed": 7}
    (tmp_path / J.PLAN_FILE).write_text(
        json.dumps({"plan_sha256": J.plan_digest(other), "plan": other}, default=str)
    )
    with pytest.raises(J.JournalRefused) as caught:
        J.Journal.open(tmp_path, plan_sha256=None)
    assert caught.value.code == "E_PLAN"
    # The refusal left no writer behind.
    (tmp_path / J.PLAN_FILE).unlink()
    J.Journal.open(tmp_path, plan_sha256=None).close()


def test_killed_after_the_plan_before_the_header_the_run_starts_again(tmp_path):
    run = tmp_path / "run"
    assert _child(run, "after_plan").returncode == -signal.SIGKILL
    kept = J.read_plan(run)
    assert kept is not None and not (run / J.JOURNAL).exists()
    with pytest.raises(J.JournalRefused) as caught:
        J.Journal.open(run, plan_sha256=kept["plan_sha256"])
    assert caught.value.code == "E_EMPTY"
    J.Journal.create(
        run,
        run_id="r-header-crash",
        plan_sha256=kept["plan_sha256"],
        authority=who(),
        plan=kept["plan"],
        **CHILD_MODES,
    ).close()
    assert J.Journal.read(run).events[0].header.plan_sha256 == kept["plan_sha256"]


# --- review JNL1 -------------------------------------------------------------------------


def test_a_plan_whose_digest_changes_on_disk_is_refused_before_the_header(tmp_path):
    """Keys that are not text come back from JSON as text and sort another
    way: the digest of the plan read back differs, so the run would never
    open again. It is refused before anything is written (review F1)."""
    plan = {"steps": {2: "b", 10: "a"}}
    with pytest.raises(J.JournalRefused) as caught:
        J.Journal.create(
            tmp_path,
            run_id=RUN,
            plan_sha256=J.plan_digest(plan),
            authority=who(),
            plan=plan,
        )
    assert caught.value.code == "E_PLAN"
    assert not (tmp_path / J.PLAN_FILE).exists()
    assert not (tmp_path / J.JOURNAL).exists()
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_the_same_mismatch_is_noted_once_however_often_it_is_refused(tmp_path):
    """A supervisor that restarts a misconfigured run again and again does
    not grow its journal (review F2); another mismatch is noted too."""
    new(tmp_path, **MODES).close()
    wrong = {**MODES, "reset_mode": "human_assisted"}
    for _ in range(5):
        with pytest.raises(J.JournalRefused):
            reopen(tmp_path, **wrong, authority=who("recovery"))
    other = {**MODES, "scene_check": "operator_attested"}
    for _ in range(3):
        with pytest.raises(J.JournalRefused):
            reopen(tmp_path, **other, authority=who("recovery"))
    # A good restart in between does not make the old mismatch new again.
    with reopen(tmp_path, **MODES) as journal:
        journal.note("restarted", authority=who())
    with pytest.raises(J.JournalRefused):
        reopen(tmp_path, **wrong, authority=who("recovery"))
    notes = [
        e.note.detail
        for e in J.Journal.read(tmp_path).events
        if e.record == "note" and e.note.code == "run_header_mismatch"
    ]
    assert len(notes) == 2 and len(set(notes)) == 2
    assert "reset_mode" in notes[0] and "scene_check" in notes[1]


def test_a_damaged_plan_file_says_how_to_go_on(tmp_path):
    planned(tmp_path).close()
    (tmp_path / J.PLAN_FILE).write_bytes(b"not json")
    with pytest.raises(J.JournalRefused) as caught:
        J.Journal.open(tmp_path, plan_sha256=BODY_SHA)
    assert "move" in caught.value.detail and "run header" in caught.value.detail
    # Moved aside, the run reopens and gets its plan.json back.
    (tmp_path / J.PLAN_FILE).rename(tmp_path / "plan.json.damaged")
    J.Journal.open(tmp_path, plan_sha256=BODY_SHA, plan=PLAN_BODY).close()
    assert J.read_plan(tmp_path)["plan_sha256"] == BODY_SHA


def test_the_plan_file_is_written_durably(tmp_path, monkeypatch):
    """plan.json goes through write_durable (temporary file, fsync, replace,
    fsync of the folder), never a plain write (review F6)."""
    written = []
    real = J.write_durable

    def spy(path, data):
        written.append(Path(path).name)
        return real(path, data)

    monkeypatch.setattr(J, "write_durable", spy)
    planned(tmp_path).close()
    assert J.PLAN_FILE in written
