"""``levi/marvel0/aeri_journal.py``: terminal D's files, read without writing.

``fixtures/aeri_r08/`` is the real run of 18 Oct (r08): its journal (97 lines),
heartbeat, service steps and the two side files of the object list. The page
keeps of the journal only what it decides on, and never a judgement of the VLM."""

import json
import os
import shutil
import time
from pathlib import Path

from levi.marvel0 import aeri_journal as jr

FIX = Path(__file__).resolve().parent / "fixtures" / "aeri_r08"
LINES = (FIX / "state_journal.jsonl").read_bytes().splitlines(keepends=True)


def run_dir(tmp_path, upto=None, torn=b"") -> Path:
    folder = tmp_path / "run"
    (folder / "subtasks").mkdir(parents=True, exist_ok=True)
    for side in (FIX / "subtasks").iterdir():
        shutil.copy(side, folder / "subtasks" / side.name)
    (folder / jr.JOURNAL).write_bytes(b"".join(LINES[: upto or len(LINES)]) + torn)
    return folder


def read(tmp_path, upto=None, **kw) -> jr.JView:
    folder = run_dir(tmp_path, upto, **kw)
    return jr.JournalReader(folder / jr.JOURNAL, folder).poll()


def test_the_whole_real_journal_is_folded():
    # (the real journal has 97 lines, numbered 0-96)
    assert len(LINES) == 97


def test_the_facts_the_page_decides_on_come_out_of_the_real_journal(tmp_path):
    view = read(tmp_path)
    assert view.present and view.header and view.bad_lines == 0
    assert (view.seq, view.lines) == (96, 97)
    assert view.to_state == "COMPLETED" and view.reason == "operator_stop"
    assert view.commit_count == 17
    assert view.reverse_done == 1
    assert view.service_steps == 11
    assert view.released == ((26, 1, "aeri-20261018-ext-r08:round1:s26"),)
    assert view.round_released == 1
    assert (view.inventory_seq, view.confirmed_seq, view.rejected_seq) == (24, 25, -1)
    assert view.inventory_ids == (
        "blue_plate_1",
        "white_plate_1",
        "red_pepper_1",
        "purple_eggplant_1",
    )
    assert view.inventory_confirmed and not view.inventory_pending
    assert view.startup_requested_seq == 14
    assert view.verdicts == () and view.discarded == 0 and view.interrupt_seq == -1


def test_the_startup_pose_check_that_asks_for_o_p_h_is_found_though_it_is_folded(
    tmp_path,
):
    """r08's note reads "2x, first: explicit startup home confirmation required:
    ..." (D folds repeats of a note into one line): the prefix must not hide it."""
    view = read(tmp_path, upto=14)  # lines 0-13: up to the pose check
    assert view.commit_count == 0
    assert view.startup_prompt_seq == 13
    assert view.startup_requested_seq == -1
    later = read(tmp_path, upto=15)  # + the prepared line of the home
    assert later.startup_requested_seq == 14 and later.commit_count == 0
    done = read(tmp_path, upto=17)
    assert done.commit_count == 1 and done.to_state == "PREFLIGHT"
    assert done.reason == "startup_home_requested"


def test_a_pose_check_after_a_commit_is_not_the_startup_prompt(tmp_path):
    view = read(tmp_path, upto=31)
    assert view.startup_prompt_seq == 13  # (the earlier one; not overwritten)
    assert view.pose_refused_seq == -1


def test_the_journal_at_the_object_list_and_the_release(tmp_path):
    view = read(tmp_path, upto=25)  # up to the list (seq 24)
    assert view.inventory_pending and not view.inventory_confirmed
    assert view.to_state == "VERIFY_INITIAL" and view.last_commit.seq == 21
    view = read(tmp_path, upto=26)  # the list is confirmed
    assert view.inventory_confirmed and not view.inventory_pending
    assert not view.released_after(view.last_commit.seq)
    view = read(tmp_path, upto=27)  # round 1 is released
    assert view.released_after(view.last_commit.seq)


def test_the_second_release_prompt_has_no_release_after_the_last_commit(tmp_path):
    view = read(tmp_path, upto=93)  # through the pose check of seq 92
    assert view.to_state == "VERIFY_INITIAL" and view.last_commit.seq == 89
    assert view.released[-1][0] == 26 < view.last_commit.seq
    assert not view.released_after(view.last_commit.seq)
    assert view.reverse_done == 1


def test_an_open_transaction_is_the_prepared_line_without_its_commit(tmp_path):
    view = read(tmp_path, upto=32)  # seq 31 prepared, 32 not read
    assert view.open_transaction is not None and view.open_transaction.seq == 31
    assert read(tmp_path, upto=34).open_transaction is None


def test_judgements_of_the_vlm_are_never_kept(tmp_path):
    view = read(tmp_path)
    kept = set(view.notes)
    assert "subtask_check" not in kept and "subtask_plan" not in kept
    assert "subtask_retry" not in kept and "subtask_loop_done" not in kept
    assert kept <= jr.CODES
    text = json.dumps([view.notes[c].detail for c in view.notes])
    assert "attempt" not in text and "outcome" not in text


def test_a_note_that_is_not_listed_has_no_detail_in_the_events(tmp_path):
    folder = run_dir(tmp_path, upto=5)
    row = json.dumps(
        {
            "sequence_no": 5,
            "record": "note",
            "note": {"code": "subtask_check", "detail": '{"done": true}'},
        }
    )
    with open(folder / jr.JOURNAL, "ab") as handle:
        handle.write(row.encode() + b"\n")
    reader = jr.JournalReader(folder / jr.JOURNAL, folder)
    view = reader.poll()
    assert "subtask_check" not in view.notes
    assert reader._events[-1].code is None and reader._events[-1].detail is None


def test_reading_in_pieces_gives_the_same_view(tmp_path):
    folder = run_dir(tmp_path, upto=1)
    path = folder / jr.JOURNAL
    reader = jr.JournalReader(path, folder)
    data = b"".join(LINES)
    last = None
    cuts = [0, 1000, 1001, 30000, 30001, 60000, len(data)]
    for end in cuts[1:]:
        path.write_bytes(data[:end])
        last = reader.poll()
    whole = read(tmp_path / "w")
    assert last.seq == whole.seq and last.released == whole.released
    assert last.inventory_ids == whole.inventory_ids
    assert last.commit_count == whole.commit_count


def test_a_torn_last_line_is_left_for_the_next_look(tmp_path):
    folder = run_dir(tmp_path, upto=40)
    path = folder / jr.JOURNAL
    reader = jr.JournalReader(path, folder)
    assert reader.poll().seq == 39
    half = LINES[40][:50]
    with open(path, "ab") as handle:
        handle.write(half)
    assert reader.poll().seq == 39, "half a line is not an event"
    with open(path, "ab") as handle:
        handle.write(LINES[40][50:])
    assert reader.poll().seq == 40


def test_a_line_that_is_not_what_d_writes_is_counted_and_not_trusted(tmp_path):
    folder = run_dir(tmp_path, upto=3)
    path = folder / jr.JOURNAL
    with open(path, "ab") as handle:
        handle.write(b"not json\n[1, 2]\n" + b'{"record": "committed"}\n')
    view = jr.JournalReader(path, folder).poll()
    assert view.bad_lines == 3 and view.seq == 2 and view.commit_count == 0


def test_a_journal_that_became_shorter_is_followed_from_its_start(tmp_path):
    folder = run_dir(tmp_path)
    path = folder / jr.JOURNAL
    reader = jr.JournalReader(path, folder)
    assert reader.poll().seq == 96
    path.write_bytes(b"".join(LINES[:3]))
    view = reader.poll()
    assert view.seq == 2 and view.commit_count == 0


def test_a_missing_journal_is_not_present(tmp_path):
    reader = jr.JournalReader(tmp_path / "none.jsonl")
    assert reader.poll().present is False


def test_the_journal_is_not_followed_through_a_link(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_bytes(b"".join(LINES[:5]))
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    assert jr.JournalReader(link).poll().present is False


def test_the_objects_of_a_long_note_are_read_from_its_side_file(tmp_path):
    view = read(tmp_path, upto=26)
    assert view.inventory_ids and len(view.inventory_ids) == 4


def test_a_side_file_that_is_not_the_one_named_is_not_believed(tmp_path):
    folder = run_dir(tmp_path, upto=26)
    side = folder / "subtasks" / "000024-subtask_inventory.json"
    side.write_text(side.read_text().replace("red_pepper_1", "blue_cup_9", 1))
    view = jr.JournalReader(folder / jr.JOURNAL, folder).poll()
    assert view.inventory_ids is None, "the digest of the note does not fit"


def test_a_side_file_outside_the_run_folder_is_never_opened(tmp_path):
    folder = run_dir(tmp_path, upto=25)
    outside = tmp_path / "outside.json"
    outside.write_text('{"parsed": [{"id": "x"}]}')
    for name in (
        "../outside.json",
        "/etc/passwd",
        "subtasks/../../outside.json",
        "subtasks/a/b.json",
    ):
        row = json.dumps(
            {
                "sequence_no": 25,
                "record": "note",
                "note": {
                    "code": "subtask_inventory",
                    "detail": json.dumps({"file": name}),
                },
            }
        )
        with open(folder / jr.JOURNAL, "ab") as handle:
            handle.write(row.encode() + b"\n")
        view = jr.JournalReader(folder / jr.JOURNAL, folder).poll()
        assert view.inventory_ids is None, name
    # a side file that is a link is not followed either
    (folder / "subtasks" / "l.json").symlink_to(outside)
    row = json.dumps(
        {
            "sequence_no": 26,
            "record": "note",
            "note": {
                "code": "subtask_inventory",
                "detail": '{"file": "subtasks/l.json"}',
            },
        }
    )
    with open(folder / jr.JOURNAL, "ab") as handle:
        handle.write(row.encode() + b"\n")
    assert jr.JournalReader(folder / jr.JOURNAL, folder).poll().inventory_ids is None


def test_inline_items_are_taken_as_they_are(tmp_path):
    folder = tmp_path / "r"
    folder.mkdir()
    row = json.dumps(
        {
            "sequence_no": 0,
            "record": "note",
            "note": {
                "code": "subtask_inventory",
                "detail": '{"items": ["a_1", "b_2"]}',
            },
        }
    )
    (folder / jr.JOURNAL).write_text(row + "\n")
    view = jr.JournalReader(folder / jr.JOURNAL, folder).poll()
    assert view.inventory_ids == ("a_1", "b_2") and view.inventory_pending


def test_a_refused_pose_check_is_remembered(tmp_path):
    folder = tmp_path / "r"
    folder.mkdir()
    rows = [
        {"sequence_no": 0, "record": "run_header"},
        {
            "sequence_no": 1,
            "record": "committed",
            "from_state": "PREFLIGHT",
            "to_state": "VERIFY_INITIAL",
            "reason": "preflight_passed",
        },
        {
            "sequence_no": 2,
            "record": "note",
            "note": {"code": "start_pose_checked", "detail": "refused: arm 40 mm away"},
        },
    ]
    (folder / jr.JOURNAL).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    view = jr.JournalReader(folder / jr.JOURNAL, folder).poll()
    assert view.pose_refused_seq == 2 and view.startup_prompt_seq == -1


# --- the heartbeat, the steps, the queue ---------------------------------------------------


def test_the_real_heartbeat_file():
    view = jr.read_runner(FIX / "runner.json")
    assert view.present and view.state == "exited" and view.pid == 1281312
    assert view.identity == {
        "boot": "d0a9d3b5-761b-44b2-9b9d-f87f6860183d",
        "start_ticks": "76650256",
    }
    assert jr.runner_age_s(view, view.updated_wall_ns / 1e9 + 3.0) == 3.0
    assert jr.runner_age_s(jr.RView(), 5.0) is None


def test_a_broken_heartbeat_is_not_present(tmp_path):
    for body in (b"", b"{", b"[1]", b'{"pid": "x", "updated_wall_ns": 1.5}'):
        (tmp_path / "r.json").write_bytes(body)
        view = jr.read_runner(tmp_path / "r.json")
        assert view.pid is None and view.updated_wall_ns is None
    assert jr.read_runner(tmp_path / "none.json").present is False


def test_the_service_steps_of_the_real_run(tmp_path):
    reader = jr.StepsReader(FIX / "steps.jsonl")
    view = reader.poll()
    assert view.steps == 13 and view.last_key == "released" and view.last_step == 7
    assert view.failed is False
    path = tmp_path / "s.jsonl"
    path.write_bytes(b'{"step": 1, "key": "gpu", "ok": false}\n{"step"')
    mine = jr.StepsReader(path)
    assert mine.poll().steps == 1 and mine.view.failed is True


def test_the_unanswered_requests_of_an_external_run(tmp_path):
    queue = tmp_path / "queue"
    for name, answered in (
        ("0001-inventory", True),
        ("0002-plan", False),
        ("0003-check", False),
    ):
        folder = queue / name
        folder.mkdir(parents=True)
        (folder / "prompt.txt").write_text("q")
        if answered:
            (folder / "answer.json").write_text("{}")
    os.utime(queue / "0002-plan" / "prompt.txt", (time.time() - 100,) * 2)
    (queue / "0004-empty").mkdir()  # no prompt yet: not a request
    pending, oldest = jr.queue_state(queue, time.time())
    assert pending == 2 and 99 <= oldest <= 102
    assert jr.queue_state(tmp_path / "nope", time.time()) == (0, None)
