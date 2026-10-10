"""The AERI run journal (levi/automatic/journal.py): durable appends, the
hash chain, transaction rules, idempotency, a single writer, and recovery
after torn writes and killed processes."""

import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from levi.automatic import journal as J
from levi.domain import aeri

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CHILD = HERE / "journal_child.py"
RUN = "r-test"
PLAN = "ab" * 32
EPISODE = f"{RUN}.forward.0001"


def who(kind="orchestrator", command_id=None):
    return {
        "principal_kind": kind,
        "principal_id": f"{kind}-1",
        "session_id": "s-1",
        "process": J.process_identity(),
        "command_id": command_id,
    }


def new(directory, **kwargs):
    return J.Journal.create(
        directory, run_id=RUN, plan_sha256=PLAN, authority=who(), **kwargs
    )


def step(journal, to_state, reason, *, kind="none", epoch=1, **kwargs):
    journal.prepare(
        to_state, reason, kind=kind, authority=who(), control_epoch=epoch, **kwargs
    )
    if kind != "none":
        journal.acknowledge("yes", "robot_server", "ok", authority=who())
    return journal.commit(authority=who())


def sample(directory, *, complete=True):
    """A small run: header, two transitions, a motion, a note, (COMPLETED)."""
    with new(directory) as journal:
        step(journal, "VERIFY_INITIAL", "preflight_passed")
        step(
            journal,
            "FORWARD_ACTIVE",
            "scene_ready",
            kind="policy_steps",
            epoch=2,
            non_idempotent=True,
            step=0,
            episode_id=EPISODE,
            episode_role="forward",
        )
        journal.note("judgement_dropped_stale", "j-1 after expiry", authority=who())
        if complete:
            journal.prepare(
                "COMPLETED",
                "run_completed",
                kind="none",
                authority=who(),
                control_epoch=2,
            )
            journal.commit(authority=who())
    return (Path(directory) / J.JOURNAL).read_bytes()


def assert_sound(directory):
    """Whole lines only, chained, every rule kept."""
    found = J.Journal.read(directory)
    assert found.corrupt is None and found.torn == b""
    previous = J.ZEROS
    for index, (event, line) in enumerate(zip(found.events, found.lines)):
        assert event.sequence_no == index and event.prev_sha256 == previous
        previous = J.line_sha(line)
    return found


# --- basics ---------------------------------------------------------------------------


def test_create_append_and_reopen(tmp_path):
    raw = sample(tmp_path)
    found = assert_sound(tmp_path)
    assert [e.record for e in found.events] == [
        "run_header",
        "prepared",
        "committed",
        "prepared",
        "acknowledged",
        "committed",
        "note",
        "prepared",
        "committed",
    ]
    header = found.events[0].header
    assert header.plan_sha256 == PLAN
    assert {c.schema_id for c in header.contracts} == set(aeri.SCHEMAS.values())
    assert found.replay.completed and found.replay.state == "COMPLETED"
    # Every line is the canonical form of its event.
    assert raw.split(b"\n")[:-1] == [aeri.dump(e) for e in found.events]
    with J.Journal.open(tmp_path, plan_sha256=PLAN) as journal:
        assert journal.state == "COMPLETED" and journal.next_seq == 9
        assert journal.recover(authority=who("recovery")).state == "COMPLETED"
    assert (tmp_path / J.JOURNAL).read_bytes() == raw


def test_snapshot_is_derived_and_rebuilt(tmp_path):
    sample(tmp_path, complete=False)
    snapshot = json.loads((tmp_path / J.SNAPSHOT).read_text())
    assert snapshot["state"] == "FORWARD_ACTIVE" and snapshot["sequence_no"] == 5
    (tmp_path / J.SNAPSHOT).unlink()
    with J.Journal.open(tmp_path, plan_sha256=PLAN) as journal:
        assert journal.state == "FORWARD_ACTIVE"  # from the journal, not the snapshot
        step(journal, "FORWARD_STOPPING", "goal_verified", kind="hold", epoch=2)
    assert (
        json.loads((tmp_path / J.SNAPSHOT).read_text())["state"] == "FORWARD_STOPPING"
    )


def test_snapshot_failure_does_not_fail_the_commit(tmp_path, monkeypatch):
    journal = new(tmp_path)

    def fail(path, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(J, "write_durable", fail)
    event = step(journal, "VERIFY_INITIAL", "preflight_passed")
    assert event.record == "committed" and journal.snapshot_error
    journal.close()
    assert assert_sound(tmp_path).replay.state == "VERIFY_INITIAL"


def test_create_refuses_an_existing_run_and_open_checks_the_plan(tmp_path):
    sample(tmp_path)
    with pytest.raises(J.JournalRefused) as caught:
        new(tmp_path)
    assert caught.value.code == "E_EXISTS"
    with pytest.raises(J.JournalRefused) as caught:
        J.Journal.open(tmp_path, plan_sha256="ef" * 32)
    assert caught.value.code == "E_PLAN"
    with pytest.raises(J.JournalRefused) as caught:
        J.Journal.open(tmp_path / "nothing", plan_sha256=PLAN)
    assert caught.value.code == "E_EMPTY"


def test_reserved_fields_cannot_be_forged(tmp_path):
    with new(tmp_path) as journal:
        for field in ("prev_sha256", "sequence_no", "run_id", "mono_ns"):
            with pytest.raises(J.JournalRefused) as caught:
                journal.append("note", authority=who(), control_epoch=0, **{field: 1})
            assert caught.value.code == "E_CONTRACT"


# --- transaction rules ------------------------------------------------------------------


def _refused(code, call, *args, **kwargs):
    with pytest.raises(J.JournalRefused) as caught:
        call(*args, **kwargs)
    assert caught.value.code == code, str(caught.value)


def test_transaction_rules(tmp_path):
    journal = new(tmp_path)
    _refused("E_PROTOCOL", journal.commit, authority=who())
    _refused("E_PROTOCOL", journal.abort, authority=who())
    journal.prepare(
        "VERIFY_INITIAL",
        "preflight_passed",
        kind="none",
        authority=who(),
        control_epoch=1,
    )
    # One transaction at a time.
    _refused(
        "E_PROTOCOL",
        journal.prepare,
        "WAIT_HUMAN",
        "scene_unknown",
        kind="none",
        authority=who(),
        control_epoch=1,
    )
    journal.commit(authority=who())
    # A commit cannot be repeated, and a transaction needs a real from-state.
    _refused("E_PROTOCOL", journal.commit, authority=who())
    _refused(
        "E_PROTOCOL",
        journal.append,
        "prepared",
        transaction_id=f"{RUN}:tx{journal.next_seq}",
        from_state="RESET_ACTIVE",
        to_state="FAULT_LOCKED",
        reason="safety_stop",
        authority=who(),
        control_epoch=1,
        action={
            "kind": "none",
            "idempotency_key": J.action_key(RUN, None, "none", None),
            "non_idempotent": False,
            "params_sha256": "0" * 64,
        },
    )
    # A real action is committed only after it is acknowledged as executed.
    journal.prepare(
        "FORWARD_ACTIVE",
        "scene_ready",
        kind="policy_steps",
        non_idempotent=True,
        step=0,
        authority=who(),
        control_epoch=2,
        episode_id=EPISODE,
        episode_role="forward",
    )
    _refused("E_PROTOCOL", journal.commit, authority=who())
    journal.acknowledge("no", "robot_server", "refused", authority=who())
    _refused(
        "E_PROTOCOL", journal.acknowledge, "yes", "robot_server", "ok", authority=who()
    )
    _refused("E_PROTOCOL", journal.commit, authority=who())
    journal.abort(authority=who())
    assert journal.state == "VERIFY_INITIAL" and journal.open_transaction is None
    # The control epoch never goes back.
    _refused(
        "E_PROTOCOL",
        journal.prepare,
        "WAIT_HUMAN",
        "scene_unknown",
        kind="none",
        authority=who(),
        control_epoch=1,
    )
    # Nothing is prepared after COMPLETED.
    journal.prepare(
        "COMPLETED", "run_completed", kind="none", authority=who(), control_epoch=2
    )
    journal.commit(authority=who())
    _refused(
        "E_PROTOCOL",
        journal.prepare,
        "PREFLIGHT",
        "human_resumed",
        kind="none",
        authority=who(),
        control_epoch=3,
    )
    journal.close()
    assert_sound(tmp_path)


def test_non_idempotent_action_is_never_prepared_twice(tmp_path):
    journal = new(tmp_path)
    step(journal, "VERIFY_INITIAL", "preflight_passed")
    motion = {
        "kind": "policy_steps",
        "non_idempotent": True,
        "step": 0,
        "authority": who(),
        "control_epoch": 2,
        "episode_id": EPISODE,
        "episode_role": "forward",
    }
    first = journal.prepare("FORWARD_ACTIVE", "scene_ready", **motion)
    journal.acknowledge("unknown", "robot_server", "timeout", authority=who())
    journal.abort(authority=who())
    assert journal.attempted(first.action.idempotency_key) == first.transaction_id
    _refused("E_PROTOCOL", journal.prepare, "FORWARD_ACTIVE", "scene_ready", **motion)
    # An idempotent action may be tried again.
    hold = {"kind": "hold", "authority": who(), "control_epoch": 2, "step": 0}
    journal.prepare("WAIT_HUMAN", "scene_unknown", **hold)
    journal.abort(authority=who())
    journal.prepare("WAIT_HUMAN", "scene_unknown", **hold)
    journal.close()


def test_expected_seq_is_a_compare_and_set_and_commands_are_found(tmp_path):
    journal = new(tmp_path)
    operator = who("operator", command_id="cmd-7")
    _refused(
        "E_SEQ",
        journal.prepare,
        "WAIT_HUMAN",
        "operator_stop",
        kind="none",
        authority=operator,
        control_epoch=1,
        expected_seq=5,
    )
    journal.prepare(
        "WAIT_HUMAN",
        "operator_stop",
        kind="none",
        authority=operator,
        control_epoch=1,
        expected_seq=1,
    )
    journal.commit(authority=operator)
    assert [e.record for e in journal.by_command("cmd-7")] == ["prepared", "committed"]
    assert journal.by_command("cmd-8") == []
    assert [e.record for e in journal.transaction(f"{RUN}:tx1")] == [
        "prepared",
        "committed",
    ]
    journal.close()


def test_concurrent_threads_append_one_chain(tmp_path):
    journal = new(tmp_path)
    errors = []

    def worker(n):
        try:
            for i in range(25):
                journal.note("heartbeat", f"{n}-{i}", authority=who())
        except J.JournalError as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    journal.close()
    assert errors == []
    found = assert_sound(tmp_path)
    assert len(found.events) == 1 + 8 * 25
    assert len({e.note.detail for e in found.events[1:]}) == 200


def test_racing_prepares_cannot_both_win(tmp_path):
    journal = new(tmp_path)
    results = []

    def attempt():
        try:
            results.append(
                journal.prepare(
                    "VERIFY_INITIAL",
                    "preflight_passed",
                    kind="none",
                    authority=who(),
                    control_epoch=1,
                )
            )
        except J.JournalRefused as exc:
            results.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    journal.close()
    assert sum(isinstance(r, aeri.RunEvent) for r in results) == 1
    assert_sound(tmp_path)


# --- one writer -------------------------------------------------------------------------


def test_second_writer_in_this_process_is_refused(tmp_path):
    journal = new(tmp_path)
    with pytest.raises(J.JournalBusy) as caught:
        J.Journal.open(tmp_path, plan_sha256=PLAN)
    assert caught.value.holder["pid"] == os.getpid()
    journal.close()
    J.Journal.open(tmp_path, plan_sha256=PLAN).close()


def _child(*args, **kwargs):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [env.get("PYTHONPATH"), str(ROOT)])
    )
    return subprocess.Popen(
        [sys.executable, str(CHILD), *map(str, args)], env=env, **kwargs
    )


def test_writer_in_another_process_holds_the_lock_until_it_dies(tmp_path):
    child = _child(tmp_path, "hold", stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(J.JournalBusy) as caught:
            J.Journal.open(tmp_path, plan_sha256=J_PLAN_CHILD)
        assert caught.value.holder["pid"] == child.pid
        assert caught.value.holder["start_ticks"] > 0
    finally:
        child.kill()
        child.wait(10)
        child.stdout.close()
    # The kernel dropped the dead holder's lock.
    with J.Journal.open(tmp_path, plan_sha256=J_PLAN_CHILD) as journal:
        assert journal.recover(authority=who("recovery")).state == "FAULT_LOCKED"


J_PLAN_CHILD = "cd" * 32


# --- crash recovery ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("point", "executed", "dangling"),
    [
        ("before_prepared", 0, False),
        ("after_prepared", 0, True),
        ("after_execute", 1, True),
        ("after_acknowledged", 1, True),
        ("after_committed", 1, False),
    ],
)
def test_killed_at_every_crash_point_recovers_to_fault_locked(
    tmp_path, point, executed, dangling
):
    run = tmp_path / "run"
    counter = tmp_path / "commands"
    counter.touch()
    child = _child(run, f"crash:{point}", counter)
    assert child.wait(60) == -signal.SIGKILL
    assert counter.read_bytes().count(b"x") == executed
    before = J.Journal.read(run)
    assert before.corrupt is None
    with J.Journal.open(run, plan_sha256=J_PLAN_CHILD) as journal:
        result = journal.recover(authority=who("recovery"))
        assert (result.state, result.reason) == ("FAULT_LOCKED", "recovery_ambiguous")
        assert (result.dangling is not None) is dangling
        assert journal.state == "FAULT_LOCKED"
        records = [e.record for e in result.written]
        expected = ["prepared", "committed"]
        if dangling:
            expected = ["aborted", "note", *expected]
            note = result.written[1]
            assert note.note.code == "crash_before_commit"
            assert note.transaction_id == result.dangling
        assert records == expected
        # The new control epoch fences every token issued before the crash.
        assert journal.control_epoch == before.replay.control_epoch + 1
        # Recovery acts on nothing: only ``none`` actions were prepared.
        assert all(e.action.kind == "none" for e in result.written if e.action)
        # An operator resumes and the run comes back to where the motion
        # was prepared. Once prepared, the motion is never prepared again,
        # in any later control epoch; never prepared, it may be.
        operator = who("operator", command_id="cmd-resume")
        journal.prepare(
            "PREFLIGHT",
            "human_resumed",
            kind="none",
            authority=operator,
            control_epoch=journal.control_epoch + 1,
        )
        journal.commit(authority=operator)
        step(journal, "VERIFY_INITIAL", "preflight_passed", epoch=journal.control_epoch)
        again = {
            "kind": "policy_steps",
            "non_idempotent": True,
            "step": 0,
            "authority": who(),
            "control_epoch": journal.control_epoch,
            "episode_id": "r-crash.forward.0001",
            "episode_role": "forward",
        }
        if point == "before_prepared":
            journal.prepare("FORWARD_ACTIVE", "scene_ready", **again)
        else:
            _refused(
                "E_PROTOCOL", journal.prepare, "FORWARD_ACTIVE", "scene_ready", **again
            )
    assert counter.read_bytes().count(b"x") == executed
    assert_sound(run)


def test_restarting_again_and_again_stays_fault_locked(tmp_path):
    sample(tmp_path, complete=False)
    for _ in range(3):
        with J.Journal.open(tmp_path, plan_sha256=PLAN) as journal:
            result = journal.recover(authority=who("recovery"))
            assert result.state == "FAULT_LOCKED"
    found = assert_sound(tmp_path)
    assert found.replay.state == "FAULT_LOCKED"
    assert found.replay.control_epoch == 2 + 3


def test_recovery_needs_a_recovery_principal(tmp_path):
    sample(tmp_path, complete=False)
    with J.Journal.open(tmp_path, plan_sha256=PLAN) as journal:
        _refused("E_PROTOCOL", journal.recover, authority=who())


def test_killed_while_appending_leaves_whole_lines(tmp_path):
    rng = random.Random(7)
    for trial in range(4):
        run = tmp_path / f"run{trial}"
        child = _child(run, "notes")
        deadline = time.monotonic() + 20
        while not (run / J.JOURNAL).exists() or (run / J.JOURNAL).stat().st_size < 2000:
            assert time.monotonic() < deadline and child.poll() is None
            time.sleep(0.01)
        time.sleep(rng.uniform(0.0, 0.2))
        child.send_signal(signal.SIGKILL)
        child.wait(10)
        found = J.Journal.read(run)
        assert found.corrupt is None and len(found.events) > 1
        with J.Journal.open(run, plan_sha256=J_PLAN_CHILD) as journal:
            journal.recover(authority=who("recovery"))
        assert_sound(run)


def _expected_states(raw):
    """State after each whole-line prefix of a sound journal."""
    states, replay = [], J.Replay()
    for line in raw.split(b"\n")[:-1]:
        replay.apply(aeri.parse(line, "run_event"))
        states.append((replay.state, replay.completed))
    return states


def test_truncation_at_every_byte_is_read_consistently(tmp_path):
    raw = sample(tmp_path / "source")
    states = _expected_states(raw)
    scratch = tmp_path / "cut"
    scratch.mkdir()
    for size in range(len(raw) + 1):
        prefix = raw[:size]
        (scratch / J.JOURNAL).write_bytes(prefix)
        found = J.Journal.read(scratch)
        whole = prefix.count(b"\n")
        assert found.corrupt is None, size
        assert len(found.events) == whole, size
        assert found.good_bytes + len(found.torn) == size
        if whole:
            assert (found.replay.state, found.replay.completed) == states[whole - 1]


def test_truncation_then_recovery_is_consistent(tmp_path):
    raw = sample(tmp_path / "source")
    states = _expected_states(raw)
    ends = [i + 1 for i, byte in enumerate(raw) if byte == 0x0A]
    rng = random.Random(11)
    sizes = {0, len(raw)}
    for end in ends:
        sizes.update({end - 1, end, end + 1})
    sizes.update(rng.randrange(len(raw)) for _ in range(12))
    for size in sorted(s for s in sizes if 0 <= s <= len(raw)):
        run = tmp_path / f"cut{size}"
        run.mkdir()
        (run / J.JOURNAL).write_bytes(raw[:size])
        whole = raw[:size].count(b"\n")
        torn = raw[ends[whole - 1] if whole else 0 : size]
        if whole == 0:
            # Not even a header: the run never started; it may be created.
            with pytest.raises(J.JournalRefused) as caught:
                J.Journal.open(run, plan_sha256=PLAN)
            assert caught.value.code == "E_EMPTY"
            new(run).close()
        else:
            with J.Journal.open(run, plan_sha256=PLAN) as journal:
                result = journal.recover(authority=who("recovery"))
            completed = states[whole - 1][1]
            assert result.state == ("COMPLETED" if completed else "FAULT_LOCKED"), size
        assert_sound(run)
        kept = sorted((run / J.TORN).glob("*.bin")) if (run / J.TORN).exists() else []
        assert [p.read_bytes() for p in kept] == ([torn] if torn else []), size


def test_a_whole_last_line_with_a_broken_chain_is_corrupt_not_torn(tmp_path):
    # A crash leaves a line without its newline, never a whole decodable
    # line that does not chain: that is an edit, and nothing is cut.
    raw = sample(tmp_path, complete=False)
    lines = raw.split(b"\n")[:-1]
    event = json.loads(lines[-1])
    event["prev_sha256"] = "f" * 64
    forged = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    damaged = b"\n".join([*lines[:-1], forged]) + b"\n"
    (tmp_path / J.JOURNAL).write_bytes(damaged)
    found = J.Journal.read(tmp_path)
    assert found.corrupt and found.corrupt_code == "E_CHAIN" and found.torn == b""
    with J.Journal.open(tmp_path, plan_sha256=PLAN) as journal:
        assert journal.corrupt
    assert (tmp_path / J.JOURNAL).read_bytes() == damaged


def test_an_edited_second_to_last_line_does_not_drop_the_completed_commit(tmp_path):
    # The review's case: the prepared COMPLETED line is edited (still valid,
    # still chained to its predecessor); the committed line after it no
    # longer chains. It used to be cut as torn, losing COMPLETED.
    raw = sample(tmp_path)
    lines = raw.split(b"\n")[:-1]
    event = json.loads(lines[-2])
    event["emitted_wall_ns"] += 1
    lines[-2] = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    damaged = b"\n".join(lines) + b"\n"
    (tmp_path / J.JOURNAL).write_bytes(damaged)
    found = J.Journal.read(tmp_path)
    assert found.corrupt and found.torn == b""
    assert found.effective_state == "FAULT_LOCKED"
    with J.Journal.open(tmp_path, plan_sha256=PLAN) as journal:
        assert journal.state == "FAULT_LOCKED"
        assert journal.recover(authority=who("recovery")).reason == "journal_corrupt"
    assert (tmp_path / J.JOURNAL).read_bytes() == damaged
    assert not (tmp_path / J.TORN).exists()


def test_a_corrupt_journal_reports_fault_locked_as_its_state(tmp_path):
    raw = sample(tmp_path, complete=False)
    lines = raw.split(b"\n")[:-1]
    lines[2] = b"{not json"
    (tmp_path / J.JOURNAL).write_bytes(b"\n".join(lines) + b"\n")
    with J.Journal.open(tmp_path, plan_sha256=PLAN) as journal:
        assert journal.state == "FAULT_LOCKED"
        assert not journal.completed


@pytest.mark.parametrize("damage", ["flip", "garbage"])
def test_a_bad_line_before_the_last_makes_the_journal_corrupt(tmp_path, damage):
    raw = sample(tmp_path, complete=False)
    lines = raw.split(b"\n")[:-1]
    if damage == "flip":
        lines[2] = lines[2].replace(b'"mono_ns":', b'"mono_ns":1', 1)
    else:
        lines[2] = b"{not json"
    damaged = b"\n".join(lines) + b"\n"
    (tmp_path / J.JOURNAL).write_bytes(damaged)
    with J.Journal.open(tmp_path, plan_sha256=PLAN) as journal:
        assert journal.corrupt
        result = journal.recover(authority=who("recovery"))
        assert (result.state, result.reason) == ("FAULT_LOCKED", "journal_corrupt")
        assert result.written == []
        _refused("E_CORRUPT", journal.note, "x", authority=who())
    assert (tmp_path / J.JOURNAL).read_bytes() == damaged


def test_whole_chained_lines_that_break_the_rules_are_corrupt(tmp_path):
    # An edited journal: a commit with no prepared transaction, properly
    # chained. Not a crash: refused as corrupt even as the last line.
    raw = sample(tmp_path / "a", complete=False)
    lines = raw.split(b"\n")[:-1]
    header = aeri.parse(lines[0], "run_event")
    commit = aeri.parse(lines[2], "run_event")
    forged = commit.model_copy(
        update={"sequence_no": 1, "prev_sha256": J.line_sha(lines[0])}
    )
    run = tmp_path / "b"
    run.mkdir()
    (run / J.JOURNAL).write_bytes(aeri.dump(header) + b"\n" + aeri.dump(forged) + b"\n")
    found = J.Journal.read(run)
    assert found.corrupt and "not the open transaction" in found.corrupt


def test_a_failed_write_stops_the_writer_and_reopening_heals(tmp_path, monkeypatch):
    journal = new(tmp_path)
    step(journal, "VERIFY_INITIAL", "preflight_passed")
    real_write = os.write

    def half(fd, data):
        real_write(fd, bytes(data[: len(data) // 2]))
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(J.os, "write", half)
    _refused("E_BROKEN", journal.note, "x", authority=who())
    monkeypatch.setattr(J.os, "write", real_write)
    _refused("E_BROKEN", journal.note, "y", authority=who())
    journal.close()
    assert J.Journal.read(tmp_path).torn
    with J.Journal.open(tmp_path, plan_sha256=PLAN) as journal:
        assert journal.state == "VERIFY_INITIAL" and journal.next_seq == 3
        journal.note("after_heal", authority=who())
    assert_sound(tmp_path)
    assert len(list((tmp_path / J.TORN).glob("*.bin"))) == 1


def test_action_keys_are_stable():
    key = J.action_key(RUN, EPISODE, "policy_steps", 12)
    assert key == J.action_key(RUN, EPISODE, "policy_steps", 12)
    assert key != J.action_key(RUN, EPISODE, "policy_steps", 13)
    assert len(key) == 64


def test_removing_the_lock_file_does_not_let_a_second_writer_in(tmp_path):
    journal = new(tmp_path)
    (tmp_path / J.LOCK).unlink()
    with pytest.raises(J.JournalBusy):
        J.Journal.open(tmp_path, plan_sha256=PLAN)
    journal.close()
    J.Journal.open(tmp_path, plan_sha256=PLAN).close()


def test_a_refused_header_leaves_no_writer_behind(tmp_path):
    with pytest.raises(J.JournalRefused):
        J.Journal.create(tmp_path, run_id="bad id!", plan_sha256=PLAN, authority=who())
    # The lock was released: a proper run can start here.
    new(tmp_path).close()


def test_a_non_idempotent_action_is_never_resent_after_recovery(tmp_path):
    # The review's replay: a home is prepared, the orchestrator dies, the
    # recovery moves to a new control epoch, an operator resumes, and the
    # same home comes up again. It must be refused.
    journal = new(tmp_path)
    step(journal, "VERIFY_INITIAL", "preflight_passed")
    home = {
        "kind": "home",
        "non_idempotent": True,
        "step": 0,
        "episode_id": EPISODE,
        "episode_role": "forward",
    }
    journal.prepare(
        "ROBOT_HOME", "goal_verified", authority=who(), control_epoch=2, **home
    )
    journal.close()
    with J.Journal.open(tmp_path, plan_sha256=PLAN) as journal:
        journal.recover(authority=who("recovery"))
        operator = who("operator", command_id="cmd-1")
        journal.prepare(
            "PREFLIGHT",
            "human_resumed",
            kind="none",
            authority=operator,
            control_epoch=journal.control_epoch + 1,
        )
        journal.commit(authority=operator)
        step(journal, "VERIFY_INITIAL", "preflight_passed", epoch=journal.control_epoch)
        _refused(
            "E_PROTOCOL",
            journal.prepare,
            "ROBOT_HOME",
            "goal_verified",
            authority=who(),
            control_epoch=journal.control_epoch,
            **home,
        )
        # The key does not depend on the control epoch.
        prepared = journal.events[3]
        assert prepared.action.kind == "home"
        assert J.action_key(RUN, EPISODE, "home", 0) == prepared.action.idempotency_key


def test_only_an_operator_command_leaves_fault_locked(tmp_path):
    sample(tmp_path, complete=False)
    with J.Journal.open(tmp_path, plan_sha256=PLAN) as journal:
        journal.recover(authority=who("recovery"))
        epoch = journal.control_epoch + 1
        motion = {
            "kind": "policy_steps",
            "non_idempotent": True,
            "step": 5,
            "control_epoch": epoch,
        }
        _refused(
            "E_PROTOCOL",
            journal.prepare,
            "FORWARD_ACTIVE",
            "human_resumed",
            authority=who(),
            **motion,
        )
        _refused(
            "E_PROTOCOL",
            journal.prepare,
            "PREFLIGHT",
            "human_resumed",
            kind="none",
            authority=who("operator"),
            control_epoch=epoch,
        )
        operator = who("operator", command_id="cmd-2")
        _refused(
            "E_PROTOCOL",
            journal.prepare,
            "FORWARD_ACTIVE",
            "human_resumed",
            authority=operator,
            **motion,
        )
        journal.prepare(
            "PREFLIGHT",
            "human_resumed",
            kind="none",
            authority=operator,
            control_epoch=epoch,
        )
        journal.commit(authority=operator)
        assert journal.state == "PREFLIGHT"


def test_a_commit_must_match_what_was_prepared(tmp_path):
    journal = new(tmp_path)
    tx = journal.prepare(
        "VERIFY_INITIAL",
        "preflight_passed",
        kind="none",
        authority=who(),
        control_epoch=1,
    )
    common = {
        "transaction_id": tx.transaction_id,
        "from_state": "PREFLIGHT",
        "to_state": "VERIFY_INITIAL",
        "authority": who(),
        "control_epoch": 1,
    }
    _refused("E_PROTOCOL", journal.append, "committed", reason="safety_stop", **common)
    _refused(
        "E_PROTOCOL",
        journal.append,
        "committed",
        reason="preflight_passed",
        episode_id=f"{RUN}.reset.0009",
        episode_role="reset",
        **common,
    )
    journal.append("committed", reason="preflight_passed", **common)
    journal.close()


@pytest.mark.parametrize("edit", ["minor", "unknown_field", "inconsistent"])
def test_a_whole_chained_last_line_failing_the_contract_is_corrupt_not_torn(
    tmp_path, edit
):
    raw = sample(tmp_path)
    lines = raw.split(b"\n")[:-1]
    event = json.loads(lines[-1])
    if edit == "minor":
        event["minor"] = aeri.MINORS["run_event"] + 1  # a newer writer
    elif edit == "unknown_field":
        event["added_in_a_later_minor"] = 1
    else:
        event["reason"] = None  # a commit without a reason
    lines[-1] = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    damaged = b"\n".join(lines) + b"\n"
    (tmp_path / J.JOURNAL).write_bytes(damaged)
    found = J.Journal.read(tmp_path)
    assert found.corrupt and found.torn == b""
    assert found.effective_state == "FAULT_LOCKED"
    if edit == "minor":
        assert "E_SCHEMA_TOO_NEW" in found.corrupt
    with J.Journal.open(tmp_path, plan_sha256=PLAN) as journal:
        assert journal.corrupt
        result = journal.recover(authority=who("recovery"))
        assert (result.state, result.reason) == ("FAULT_LOCKED", "journal_corrupt")
    # The committed line stays where it was: nothing is cut or moved.
    assert (tmp_path / J.JOURNAL).read_bytes() == damaged
    assert not (tmp_path / J.TORN).exists()


def test_effective_state_of_a_sound_journal_is_its_state(tmp_path):
    sample(tmp_path, complete=False)
    assert J.Journal.read(tmp_path).effective_state == "FORWARD_ACTIVE"


# --- physical actions: steps and retries (rule decided for T-C-06) -------------


def _home(journal, step, **extra):
    return journal.prepare(
        "ROBOT_HOME",
        "goal_verified",
        kind="home",
        non_idempotent=True,
        step=step,
        authority=who(),
        control_epoch=journal.control_epoch or 1,
        episode_id=EPISODE,
        episode_role="forward",
        **extra,
    )


def _back(journal):
    journal.prepare(
        "FORWARD_FINALIZE",
        "goal_verified",
        kind="none",
        authority=who(),
        control_epoch=journal.control_epoch,
    )
    journal.commit(authority=who())


def test_a_physical_action_without_a_step_is_refused(tmp_path):
    journal = new(tmp_path)
    for kind in ("home", "policy_steps"):
        _refused(
            "E_CONTRACT",
            journal.prepare,
            "ROBOT_HOME",
            "goal_verified",
            kind=kind,
            non_idempotent=True,
            step=None,
            authority=who(),
            control_epoch=1,
            episode_id=EPISODE,
            episode_role="forward",
        )
    # A home or policy steps can never be declared idempotent.
    _refused(
        "E_CONTRACT",
        journal.prepare,
        "ROBOT_HOME",
        "goal_verified",
        kind="home",
        non_idempotent=False,
        step=0,
        authority=who(),
        control_epoch=1,
    )
    journal.close()


def test_two_homes_of_one_episode_need_two_steps(tmp_path):
    # The review's first case: a second, legitimate home in the same episode
    # (after a new epoch). With steps assigned in order it is accepted.
    journal = new(tmp_path)
    _home(journal, 0)
    journal.acknowledge("yes", "robot_server", "ok", authority=who())
    journal.commit(authority=who())
    _back(journal)
    journal.prepare(
        "ROBOT_HOME",
        "goal_verified",
        kind="home",
        non_idempotent=True,
        step=1,
        authority=who(),
        control_epoch=journal.control_epoch + 1,
        episode_id=EPISODE,
        episode_role="forward",
    )
    journal.abort(authority=who())
    # Steps only go up per (episode, action): a used step is refused, and so
    # is a lower one that was never used.
    _refused("E_PROTOCOL", _home, journal, 0)
    _refused("E_PROTOCOL", _home, journal, 1)
    _home(journal, 3)
    journal.abort(authority=who())
    _refused("E_PROTOCOL", _home, journal, 2)
    journal.close()


def test_a_home_not_executed_is_retried_as_a_new_command(tmp_path):
    # The review's second case: the controller said ``executed: no``; the
    # retry is a new logical command with a new step and ``retry_of``.
    journal = new(tmp_path)
    first = _home(journal, 0)
    journal.acknowledge("no", "robot_server", "refused", authority=who())
    journal.abort(authority=who())
    _refused("E_PROTOCOL", _home, journal, 0, retry_of=first.transaction_id)
    retry = _home(journal, 1, retry_of=first.transaction_id)
    assert retry.action.retry_of == first.transaction_id
    assert retry.action.step == 1
    journal.acknowledge("unknown", "robot_server", "timeout", authority=who())
    journal.abort(authority=who())
    # Only a command known not to have run may be retried.
    _refused("E_PROTOCOL", _home, journal, 2, retry_of=retry.transaction_id)
    _refused("E_PROTOCOL", _home, journal, 2, retry_of=f"{RUN}:tx999")
    journal.close()
    assert_sound(tmp_path)
