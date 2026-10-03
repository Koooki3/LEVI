"""Aggregation of ``live/stats.jsonl`` records: the arithmetic, empty and
partial input, damaged values."""

import math

from levi.live import stats


def rec(demo, *, dataset="g__t", session="s1", at=1000.0, **parts):
    """A record; ``parts`` are dotted-path overrides, e.g. ``timeline__to_commit_s``."""
    row = stats.normalize({"dataset": dataset, "demo": demo, "session": session})
    row["at"] = at
    for dotted, value in parts.items():
        *path, leaf = dotted.split("__")
        node = row
        for key in path:
            node = node[key]
        node[leaf] = value
    return row


def full(demo, n, **parts):
    """Episode ``n`` of a session: ends at 100 + 10n, 6 s long."""
    ends = 100.0 + 10 * n
    base = {
        "attempts": 0,
        "episode__episode_seconds": 6.0,
        "timeline__completed_at": ends,
        "timeline__first_request_at": ends + 4.0,
        "timeline__to_mirror_s": 2.0 + n,
        "timeline__to_first_request_s": 4.0,
        "timeline__to_commit_s": 20.0 + n,
        "timeline__to_verdict_s": 22.0 + n,
        "model__requests__coarse": 1,
        "model__requests__refine": 1,
        "model__requests__review": 1,
        "model__requests__probe": 0,
        "model__model_seconds__coarse": 4.0,
        "model__model_seconds__refine": 2.0,
        "model__model_seconds__review": 1.0,
        "model__tokens__coarse": 1000,
        "model__tokens__refine": 500,
        "model__tokens__review": 300,
        "model__tokens__probe": 0,
        "model__prompt_tokens": 1500,
        "model__completion_tokens": 300,
        "model__total_tokens": 1800,
        "model__images": 10,
        "model__external_tokens": 0,
        "result__state": "done",
        "result__segments": 2,
        "result__segment_labels": {"approach": 1, "grasp": 1},
        "result__review": "auto",
        "result__verdict": {
            "outcome": "failure",
            "events": 0,
            "valid_events": 0,
            "undecided": False,
        },
    }
    base.update(parts)
    return rec(demo, at=ends + 25.0, **base)


def test_quantiles_interpolate_and_empty_input_has_no_numbers():
    assert stats.quantile([], 0.5) is None
    assert stats.quantile([7], 0.9) == 7
    assert stats.quantile([1, 2, 3, 4], 0.5) == 2.5
    assert math.isclose(stats.quantile([0, 10], 0.9), 9.0)
    d = stats.dist([1, 2, 3, 4, None, "x", True, float("nan"), float("inf")])
    assert d == {"n": 4, "min": 1, "median": 2.5, "p90": 3.7, "max": 4, "mean": 2.5}
    assert stats.dist([])["median"] is None and stats.dist([])["n"] == 0
    assert stats.total([None, "x"]) is None and stats.total([1, 2.5]) == 3.5
    assert stats.ratio(1, 0) is None and stats.ratio(None, 2) is None


def test_an_empty_scope_summarises_to_zero_episodes_and_nulls_not_errors():
    s = stats.summarize([])
    assert s["episodes"]["count"] == 0 and s["episodes"]["done"] == 0
    assert s["latency"]["to_commit_s"]["median"] is None
    assert s["throughput"]["realtime_factor"] is None
    assert s["model"]["tokens_total"] is None
    assert s["model"]["prompt_share"] is None
    assert s["gpu"]["batches"] == 0 and s["gpu"]["gate_window"] is None
    assert s["in_session"] == {"count": 0, "evaluable": 0, "ratio": None}
    assert stats.session_rows([]) == [] and stats.episode_rows([]) == []


def test_one_episode_gives_its_own_figures():
    s = stats.summarize([full("demo_0000", 0)])
    assert s["episodes"]["count"] == 1 and s["episodes"]["done"] == 1
    assert s["latency"]["to_commit_s"] == {
        "n": 1, "min": 20.0, "median": 20.0, "p90": 20.0, "max": 20.0, "mean": 20.0,
    }  # fmt: skip
    # 6 s of episode, 7 s of model time.
    assert s["throughput"]["realtime_factor"] == round(6 / 7, 3)
    assert s["model"]["tokens_total"] == 1800
    assert s["model"]["prompt_share"] == round(1500 / 1800, 3)
    assert s["model"]["requests_per_episode"]["median"] == 3
    assert s["model"]["images_per_episode"]["mean"] == 10
    assert s["outcome"]["segments_total"] == 2
    assert s["outcome"]["labels"] == {"approach": 1, "grasp": 1}
    assert s["outcome"]["verdicts"] == {"failure": 1}
    assert s["outcome"]["review"] == {"auto": 1}
    # One episode is its own session's last: its request is after its end.
    assert s["in_session"] == {"count": 0, "evaluable": 1, "ratio": 0.0}


def test_latency_throughput_and_overhead_over_several_episodes():
    rows = [full(f"demo_{n:04d}", n) for n in range(4)]
    s = stats.summarize(rows)
    commit = s["latency"]["to_commit_s"]  # 20, 21, 22, 23
    assert (commit["median"], commit["max"], commit["n"]) == (21.5, 23.0, 4)
    assert commit["p90"] == 22.7
    assert s["latency"]["to_mirror_s"]["median"] == 3.5
    # 24 s of episode over 28 s of model time.
    assert s["throughput"]["episode_seconds"] == 24.0
    assert s["throughput"]["model_seconds"] == 28.0
    assert s["throughput"]["realtime_factor"] == round(24 / 28, 3)
    # Wall: from the first end (100) to the last verdict (130 + 25).
    assert s["throughput"]["span_s"] == 55.0
    assert s["throughput"]["wall_factor"] == round(24 / 55, 3)
    m = s["model"]
    assert m["tokens_total"] == 7200 and m["tokens_per_episode"]["mean"] == 1800
    assert m["prompt_tokens"] == 6000 and m["completion_tokens"] == 1200
    assert m["prompt_share"] == round(6000 / 7200, 3)
    assert m["images_total"] == 40 and m["external_tokens"] == 0
    by = m["by_kind"]
    assert by["coarse"]["requests"] == 4 and by["coarse"]["seconds"] == 16.0
    assert by["coarse"]["seconds_share"] == round(16 / 28, 3)
    assert by["coarse"]["tokens_share"] == round(4000 / 7200, 3)
    assert by["probe"]["tokens"] == 0 and by["probe"]["seconds"] is None
    # Sessions: episodes 0..2 asked (at end + 4) before episode 3 ended (130).
    assert s["in_session"] == {"count": 3, "evaluable": 4, "ratio": 0.75}
    assert s["outcome"]["segment_histogram"] == {"2": 4}


def test_a_retry_replaces_its_first_attempt_for_episodes_but_not_for_cost():
    first = full("demo_0000", 0, attempts=1, result__state="mirrored")
    first["at"] = 500.0
    second = full("demo_0000", 0, attempts=2)
    second["at"] = 900.0
    s = stats.summarize([first, second])
    assert s["episodes"]["count"] == 1 and s["episodes"]["records"] == 2
    assert s["episodes"]["superseded"] == 1 and s["episodes"]["retried"] == 1
    assert s["episodes"]["done"] == 1 and s["episodes"]["retrying"] == 0
    assert s["model"]["tokens_total"] == 3600  # both attempts were paid for
    assert s["model"]["tokens_per_episode"]["n"] == 1


def test_missing_and_damaged_values_are_skipped_not_invented():
    old = stats.normalize({"dataset": "g__t", "demo": "demo_9", "at": 5})
    junk = rec(
        "demo_8",
        timeline__to_commit_s="slow",
        model__total_tokens=float("nan"),
        episode__episode_seconds=None,
    )
    junk["result"]["segment_labels"] = "oops"
    ok = full("demo_0000", 0)
    s = stats.summarize([old, junk, ok, "not a row", None])
    assert s["episodes"]["count"] == 3
    assert s["latency"]["to_commit_s"]["n"] == 1
    assert s["model"]["tokens_per_episode"]["n"] == 1
    assert s["outcome"]["labels"] == {"approach": 1, "grasp": 1}
    # The old record has no state: counted, but neither done nor failed.
    assert s["episodes"]["done"] == 1 and s["episodes"]["failed"] == 0
    assert s["outcome"]["verdicts"] == {"failure": 1, "none": 2}
    # realtime factor uses only the episode with both figures.
    assert s["throughput"]["realtime_factor_n"] == 1
    # Every aggregate is plain JSON.
    import json

    json.dumps(s, allow_nan=False)


def test_batches_come_from_the_batch_id_or_from_time_and_gate_figures():
    def gated(demo, at, wait, hits, batch=None):
        row = rec(demo, at=at, gate__closed_wait_s=wait, gate__interruptions=hits)
        if batch is not None:
            row["batch"] = {"id": batch, "size": 2}
        return row

    ided = [gated("a", 10, 5.0, 1, 7.0), gated("b", 10.1, 5.0, 1, 7.0)]
    assert [len(g) for g in stats.batches(ided)] == [2]
    loose = [
        gated("a", 10, 5.0, 1),
        gated("b", 10.5, 5.0, 1),  # same batch
        gated("c", 60, 5.0, 1),  # same figures but a minute later: another
        gated("d", 61, 0.0, 0),
    ]
    assert sorted(len(g) for g in stats.batches(loose)) == [1, 1, 2]
    s = stats.summarize(loose)
    assert s["gpu"]["batches"] == 3
    assert s["gpu"]["closed_wait_s"] == 10.0  # 5 s once per batch, not per demo
    assert s["gpu"]["interruptions"] == 2


def test_vllm_wakes_and_cold_starts_and_the_gate_window():
    rows = [
        full("demo_0000", 0, gate__vllm_wake_s=0.75),
        full("demo_0001", 1),
        full("demo_0002", 2, gate__vllm_wake_s=0.73, gate__vllm_cold_start_s=48.0),
    ]
    gate = [
        {"at": 90.0, "to": {"open": True, "code": "ok"}},
        {"at": 110.0, "to": {"open": False, "code": "episode_running"}},
        {"at": 118.0, "to": {"open": True, "code": "ok"}},
        {"at": 125.0, "to": {"open": False, "code": "episode_running"}},
        "damaged",
        {"at": 126.0, "to": "oops"},
    ]
    s = stats.summarize(rows, gate=gate)
    wakes = s["gpu"]["vllm_wakes"]
    assert wakes["count"] == 2 and wakes["total_s"] == 1.48 and wakes["max_s"] == 0.75
    assert s["gpu"]["vllm_cold_starts"] == {
        "count": 1, "total_s": 48.0, "median_s": 48.0, "max_s": 48.0,
    }  # fmt: skip
    assert s["gpu"]["vllm_sleeps"] is None  # not recorded: unknown, not 0
    # Window 100 (first end) .. 145 (last write = 120 + 25): closed 110..118
    # and 125..145.
    window = s["gpu"]["gate_window"]
    assert window["closed_s"] == 28.0 and window["closures"] == 2
    assert window["window_s"] == 45.0 and window["closed_share"] == round(28 / 45, 3)
    assert stats.gate_window([], 0, 10) is None
    assert stats.gate_window(gate, None, 10) is None


def test_in_session_needs_the_session_id_and_can_use_unlabelled_episodes():
    rows = [full("demo_0000", 0), full("demo_0001", 1)]
    # Episode 0 asked at 104, before episode 1 ended (110): in session.
    assert stats.in_session(rows)["count"] == 1
    # A third episode (130) not labelled yet pushes the session's end out:
    # episode 1 asked at 114, also in session.
    ends = {("g__t", "s1"): 130.0}
    assert stats.in_session(rows, ends)["count"] == 2
    # No session id: not evaluable.
    anon = [full("demo_0000", 0, session=None), full("demo_0001", 1, session=None)]
    assert stats.in_session(anon) == {"count": 0, "evaluable": 0, "ratio": None}
    # An episode that never asked is evaluable and not in session.
    quiet = full("demo_0002", 2, timeline__first_request_at=None)
    got = stats.in_session([*rows, quiet])
    assert got["evaluable"] == 3 and got["count"] == 2
    # Old records have no completed_at: nothing to say.
    old = stats.normalize({"dataset": "g__t", "demo": "x", "session": "s1"})
    assert stats.in_session([old])["evaluable"] == 0


def test_select_filters_and_the_tables_are_flat():
    a = [full(f"demo_{n:04d}", n) for n in range(3)]
    b = [
        full("demo_0000", 0, dataset="g__u", session="s2"),
        full("demo_0001", 1, dataset="g__u", session="s2"),
    ]
    rows = a + b
    assert len(stats.select(rows)) == 5
    assert len(stats.select(rows, dataset="g__u")) == 2
    assert len(stats.select(rows, session="s1")) == 3
    assert stats.select(rows, dataset="g__u", session="s1") == []
    assert all(r["at"] >= 140 for r in stats.select(rows, since=140))
    table = stats.episode_rows(rows)
    assert len(table) == 5 and table[0]["demo"] == "demo_0000"
    assert table[0]["total_tokens"] == 1800 and table[0]["requests"] == 3
    assert table[0]["model_seconds"] == 7.0
    assert [r["in_session"] for r in table[:3]] == [True, True, False]
    sessions = stats.session_rows(rows)
    assert [r["session"] for r in sessions] == ["s1", "s2"]  # newest last_at first
    one = sessions[0]
    assert one["episodes"] == 3 and one["tokens"] == 5400
    assert one["in_session_ratio"] == round(2 / 3, 3)
    assert one["failure"] == 3 and one["success"] == 0


def test_tokens_follow_the_reported_figures_and_never_total_minus_prompt():
    row = full(
        "demo_0000",
        0,
        model__prompt_tokens=1500,
        model__completion_tokens=300,
        model__total_tokens=1800,
        model__probe_tokens=700,
        model__reserved_tokens=900,
        model__unreported_steps=1,
    )
    other = full("demo_0001", 1, model__probe_tokens=None, model__reserved_tokens=None)
    s = stats.summarize([row, other])
    m = s["model"]
    # total = prompt + completion of the reported steps; the others apart.
    assert m["tokens_total"] == 3600 and m["prompt_tokens"] == 3000
    assert m["completion_tokens"] == 600
    assert m["probe_tokens"] == 700 and m["reserved_tokens"] == 900
    assert m["unreported_steps"] == 1
    # A record from before the fields existed gives null, not a derived number.
    old = stats.summarize([full("demo_0002", 2, model__probe_tokens=None)])["model"]
    assert old["probe_tokens"] is None and old["reserved_tokens"] is None
    assert old["unreported_steps"] is None
    # A split that was not reported is not made up from total - prompt.
    none = stats.summarize(
        [full("demo_0003", 3, model__completion_tokens=None, model__prompt_tokens=1500)]
    )["model"]
    assert none["completion_tokens"] is None and none["prompt_share"] is None


def test_removed_episodes_are_left_out_unless_asked_for():
    rows = [full(f"demo_{n:04d}", n) for n in range(4)]
    removed = {("g__t", "demo_0001")}
    marked = stats.mark_excluded(rows, removed)
    assert marked[1]["excluded"] is True and rows[1]["excluded"] in (None, False)
    assert [r["excluded"] for r in marked] != [True] * 4
    kept = stats.select(marked, include_excluded=False)
    assert [r["demo"] for r in kept] == ["demo_0000", "demo_0002", "demo_0003"]
    assert len(stats.select(marked)) == 4
    # A record that says so itself counts too.
    own = full("demo_0009", 9, excluded=True)
    assert stats.select([own], include_excluded=False) == []
    assert stats.summarize(kept)["episodes"]["count"] == 3
    assert stats.summarize(stats.select(marked))["episodes"]["excluded"] == 1
