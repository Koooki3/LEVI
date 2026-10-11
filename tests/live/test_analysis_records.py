"""``levi.live.analysis``: the append-only record file (torn lines, newest
wins, foreign lines), the time-segment extraction, and ``sync``
(idempotent, picks up changes, waits for an unreadable store)."""

import json
import threading

from analysis_helpers import (
    put_changes,
    put_state,
    record,
    row,
    seg,
    stats_row,
    workspace,
)

from levi.live import analysis


def test_append_and_read_round_trip(tmp_path):
    analysis.append(tmp_path, record(demo="demo_0001"))
    analysis.append(tmp_path, record(demo="demo_0002"))
    rows = analysis.read(tmp_path)
    assert [r["demo"] for r in rows] == ["demo_0001", "demo_0002"]
    assert rows[0]["schema"] == analysis.SCHEMA


def test_a_missing_file_reads_as_nothing(tmp_path):
    assert analysis.read(tmp_path) == []


def test_a_torn_last_line_is_skipped_and_the_next_append_starts_on_a_new_line(tmp_path):
    analysis.append(tmp_path, record(demo="demo_0001"))
    with analysis.path(tmp_path).open("ab") as handle:
        handle.write(b'{"schema": "levi.live.analysis.v1", "dataset": "g__t", "dem')
    assert [r["demo"] for r in analysis.read(tmp_path)] == ["demo_0001"]
    analysis.append(tmp_path, record(demo="demo_0003"))
    assert [r["demo"] for r in analysis.read(tmp_path)] == ["demo_0001", "demo_0003"]
    lines = analysis.path(tmp_path).read_text().splitlines()
    assert len(lines) == 3  # the torn line stays a line of its own


def test_foreign_and_damaged_lines_are_skipped(tmp_path):
    target = analysis.path(tmp_path)
    good = json.dumps(record(demo="demo_0001"))
    lines = [
        "not json",
        '{"schema": "levi.live.episode_stats.v1", "dataset": "g__t", "demo": "d"}',
        '["a list"]',
        '{"schema": "levi.live.analysis.v1", "dataset": "g__t"}',  # no demo
        '{"schema": "levi.live.analysis.v1", "dataset": "g__t", "demo": "x", "at": NaN}',
        good,
        "",
    ]
    target.write_text("\n".join(lines))
    assert [r["demo"] for r in analysis.read(tmp_path)] == ["demo_0001"]


def test_an_older_record_reads_with_missing_fields_as_none(tmp_path):
    analysis.path(tmp_path).write_text(
        json.dumps(
            {"schema": "levi.live.analysis.v0", "dataset": "g__t", "demo": "demo_0001"}
        )
        + "\n"
    )
    (only,) = analysis.read(tmp_path)
    assert only["segments"] is None and only["capability"] is None
    assert only["operator"] is None and only["labelled_at"] is None


def test_the_newest_line_of_an_episode_wins(tmp_path):
    analysis.append(tmp_path, record(demo="demo_0001", operator={"outcome": "failure"}))
    analysis.append(tmp_path, record(demo="demo_0002"))
    analysis.append(tmp_path, record(demo="demo_0001", operator={"outcome": "success"}))
    kept = analysis.latest(analysis.read(tmp_path))
    by_demo = {r["demo"]: r for r in kept}
    assert len(kept) == 2
    assert by_demo["demo_0001"]["operator"]["outcome"] == "success"


def test_since_drops_older_records(tmp_path):
    analysis.append(tmp_path, record(demo="demo_0001", labelled_at=10.0))
    analysis.append(tmp_path, record(demo="demo_0002", labelled_at=20.0))
    assert [r["demo"] for r in analysis.read(tmp_path, since=15)] == ["demo_0002"]


def test_concurrent_appends_never_interleave(tmp_path):
    def work(k):
        for n in range(25):
            analysis.append(tmp_path, record(demo=f"demo_{k}_{n}"))

    threads = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = analysis.path(tmp_path).read_text().splitlines()
    assert len(lines) == 100
    assert all(json.loads(line)["schema"] == analysis.SCHEMA for line in lines)
    assert len(analysis.read(tmp_path)) == 100


def test_segments_are_ordered_filtered_and_measured():
    proposals = [
        seg(3, "grasp", 2.0, 5.0, "success"),
        seg(3, "approach", 0.0, 2.0, "success"),
        seg(4, "approach", 0.0, 9.0),  # another episode
        seg(3, "place", 5.0, 5.5, "failure", style="plan"),  # not a subtask
        {"kind": "event", "episode_index": 3, "start": 1.0},
        seg(3, "retreat", 6.0, None),  # no end: no duration
        seg(3, "transport", 5.0, 6.0, "success"),
    ]
    items = analysis.segment_items(proposals, 3)
    assert [i["id"] for i in items] == ["approach", "grasp", "transport", "retreat"]
    assert [i["duration"] for i in items] == [2.0, 3.0, 1.0, None]
    assert items[0]["outcome"] == "success" and items[3]["outcome"] is None


def test_a_rejected_segment_is_left_out():
    proposals = [seg(1, "approach", 0, 1), seg(1, "grasp", 1, 2)]
    items = analysis.segment_items(proposals, 1, {"1": "rejected", "0": "accepted"})
    assert [i["id"] for i in items] == ["approach"]


def test_gaps_and_repeats():
    assert analysis.gaps(["approach", "grasp", "transport"]) == []
    assert analysis.gaps(["approach", "place"]) == ["grasp", "transport"]
    assert analysis.gaps(["custom", "other"]) == []
    assert analysis.gaps([]) == []
    assert analysis.repeats(["approach", "grasp", "approach", "grasp", "approach"]) == [
        "approach",
        "grasp",
    ]
    assert analysis.repeats(["a", "b"]) == []


def _demos(c):
    put_changes(
        c,
        "ch1",
        [seg(0, "approach", 0, 2, "success"), seg(0, "grasp", 2, 4, "failure")],
    )
    put_state(
        c,
        "g__t",
        "g",
        "t",
        {
            "demo_0000": row(0, changeset="ch1", operator="failure", verdict="failure"),
            "demo_0001": row(1, state="mirrored"),
            "demo_0002": row(2, state="failed"),
            "demo_0003": row(3, state="done", excluded={"at": 1, "by": "p"}),
        },
    )


def test_sync_writes_one_record_per_finished_episode_and_is_idempotent(tmp_path):
    c = workspace(tmp_path)
    _demos(c)
    stats_row(
        c,
        "g__t",
        "demo_0000",
        result={"spec": {"guideline": "g.md", "release_review": "r.json"}},
        capability={"levi_version": "x"},
    )
    first = analysis.sync(c, now=500.0)
    assert first["written"] == 1
    (only,) = analysis.read(c.live_dir)
    assert only["dataset"] == "g__t" and only["group"] == "g" and only["task"] == "t"
    assert only["operator"]["outcome"] == "failure"
    assert only["agent"]["review"] == "auto" and only["agent"]["reviewed"] is False
    assert only["segments"]["sequence"] == ["approach", "grasp"]
    assert only["segments"]["items"][1]["outcome"] == "failure"
    assert only["labelled_at"] == 200.0  # the commit time, not "now"
    assert only["spec"]["guideline"] == "g.md"
    assert only["capability"] == {"levi_version": "x"}
    second = analysis.sync(c, now=900.0)
    assert second["written"] == 0 and second["unchanged"] == 1
    assert len(analysis.path(c.live_dir).read_text().splitlines()) == 1


def test_sync_appends_again_only_when_the_content_changed(tmp_path):
    c = workspace(tmp_path)
    _demos(c)
    analysis.sync(c)
    # the operator corrects the label: a new line, the old one stays on file
    state = {
        "demo_0000": row(0, changeset="ch1", operator="success", verdict="failure"),
    }
    put_state(c, "g__t", "g", "t", state)
    assert analysis.sync(c)["written"] == 1
    lines = analysis.path(c.live_dir).read_text().splitlines()
    assert len(lines) == 2
    (kept,) = analysis.latest(analysis.read(c.live_dir))
    assert kept["operator"]["outcome"] == "success"


def test_sync_waits_for_a_change_set_the_store_does_not_hold_yet(tmp_path):
    c = workspace(tmp_path)
    put_state(
        c,
        "g__t",
        "g",
        "t",
        {"demo_0000": row(0, changeset="missing", operator="failure")},
    )
    out = analysis.sync(c)
    assert out["written"] == 0 and out["unreadable"] == 1
    assert analysis.read(c.live_dir) == []
    put_changes(c, "missing", [seg(0, "approach", 0, 1)])
    assert analysis.sync(c)["written"] == 1


def test_sync_does_not_record_a_demo_with_nothing_to_say(tmp_path):
    c = workspace(tmp_path)
    put_state(c, "g__t", "g", "t", {"demo_0000": row(0, operator="success")})
    out = analysis.sync(c)  # no segments pipeline, no verdict
    assert out["written"] == 0 and out["skipped"] == 1


def test_sync_can_be_limited_to_a_dataset(tmp_path):
    c = workspace(tmp_path)
    _demos(c)
    put_changes(c, "ch2", [seg(0, "approach", 0, 1)])
    put_state(c, "g__u", "g", "u", {"demo_0000": row(0, changeset="ch2")})
    assert analysis.sync(c, "g__u")["written"] == 1
    assert [r["dataset"] for r in analysis.read(c.live_dir)] == ["g__u"]


def test_two_syncs_at_once_write_each_episode_once(tmp_path):
    c = workspace(tmp_path)
    _demos(c)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(analysis.sync(c)))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r["written"] for r in results) == 1
    assert len(analysis.path(c.live_dir).read_text().splitlines()) == 1


def test_a_capability_that_is_too_large_or_not_json_is_dropped(tmp_path):
    big = {"k": "x" * 5000}
    assert analysis._capability({"capability": big}) is None
    assert analysis._capability({"capability": float("nan")}) is None
    assert analysis._capability({"capability": {"a": float("nan")}}) is None
    assert analysis._capability({}, {"capability": {"a": 1}}) == {"a": 1}


def test_controller_recovers_committed_analysis_after_worker_interruption(tmp_path):
    from levi.live import controller, mirror

    c = workspace(tmp_path)
    _demos(c)
    state_path = mirror.state_path(c, "g__t")
    before = state_path.read_bytes()
    assert analysis.read(c.live_dir) == []  # commit happened, after_batch did not

    def no_worker(*args, **kwargs):
        raise AssertionError("analysis recovery must not re-label")

    first = controller.Controller(c, popen=no_worker, log=lambda *a: None)
    first._reports(1000.0)
    first._report_thread.join(5)
    assert not first._report_thread.is_alive()
    (record_,) = analysis.read(c.live_dir)
    assert record_["demo"] == "demo_0000"
    assert record_["segments"]["sequence"] == ["approach", "grasp"]
    assert record_["operator"]["outcome"] == "failure"
    assert state_path.read_bytes() == before
    contents = analysis.path(c.live_dir).read_bytes()

    # Another controller process sees the same durable state after a restart.
    restarted = controller.Controller(c, popen=no_worker, log=lambda *a: None)
    restarted._reports(2000.0)
    restarted._report_thread.join(5)
    restarted._reports(2030.0)
    restarted._report_thread.join(5)
    assert analysis.path(c.live_dir).read_bytes() == contents
    assert state_path.read_bytes() == before


def test_controller_sync_waits_for_worker_and_survives_report_failure(
    tmp_path, monkeypatch
):
    from levi.live import controller, report

    c = workspace(tmp_path)
    _demos(c)
    ctl = controller.Controller(c, log=lambda *a: None)
    ctl.worker = object()
    ctl._reports(1000.0)
    assert ctl._report_thread is None
    assert analysis.read(c.live_dir) == []

    def fail_report(*args, **kwargs):
        raise OSError("synthetic report failure")

    monkeypatch.setattr(report, "auto", fail_report)
    ctl.worker = None
    ctl._reports(1001.0)
    ctl._report_thread.join(5)
    assert len(analysis.read(c.live_dir)) == 1
    old_thread = ctl._report_thread
    ctl._reports(1002.0)
    assert ctl._report_thread is old_thread
