"""``live/stats.jsonl``: one structured record per labelled demo."""

import json

from test_live_gpu import live, serve, wait_for  # noqa: F401  (fixtures, helper)
from test_live_pipeline import NAME, env  # noqa: F401  (fixture)

from levi.live import gating, gpumgr, stats, worker


def leaves(value, prefix=""):
    """Every dotted path of a nested dict (the schema's fields)."""
    if not isinstance(value, dict):
        return [prefix]
    return [p for k, v in value.items() for p in leaves(v, f"{prefix}.{k}".strip("."))]


def test_the_schema_is_fixed_and_a_record_is_filled_with_nulls():
    row = stats.normalize({"demo": "demo_0001", "model": {"requests": {"coarse": 2}}})
    assert row["schema"] == "levi.live.episode_stats.v1"
    assert row["demo"] == "demo_0001" and row["dataset"] is None
    assert row["model"]["requests"] == {
        "coarse": 2,
        "refine": None,
        "review": None,
        "probe": None,
    }
    assert row["result"]["verdict"]["outcome"] is None
    assert sorted(leaves(row)) == sorted(leaves(stats.TEMPLATE))
    # Extra fields of a newer writer survive; a wrong shape does not crash.
    assert stats.normalize({"new": 1})["new"] == 1
    assert stats.normalize({"episode": "oops"})["episode"]["frames"] is None
    assert stats.normalize([1]) is None
    # The fields promised in docs/LIVE.md, by name.
    assert sorted(leaves(stats.TEMPLATE)) == sorted(
        [
            "schema", "at", "dataset", "demo", "episode_index", "session",
            "attempts", "excluded",
            "episode.frames", "episode.episode_seconds",
            "timeline.to_mirror_s", "timeline.to_plan_s",
            "timeline.to_first_request_s", "timeline.to_commit_s",
            "timeline.to_verdict_s",
            "model.requests.coarse", "model.requests.refine",
            "model.requests.review", "model.requests.probe",
            "model.model_seconds.coarse", "model.model_seconds.refine",
            "model.model_seconds.review", "model.model_seconds.probe",
            "model.prompt_tokens", "model.completion_tokens",
            "model.total_tokens", "model.images", "model.external_tokens",
            "gate.closed_wait_s", "gate.interruptions", "gate.vllm_wake_s",
            "gate.vllm_cold_start_s",
            "result.state", "result.reason", "result.segments",
            "result.segment_labels", "result.verdict.outcome",
            "result.verdict.events", "result.verdict.valid_events",
            "result.verdict.undecided", "result.review", "result.spec",
            "result.provider", "result.model",
        ]
    )  # fmt: skip


def test_usage_is_summed_by_kind_from_the_journals():
    def step(episode, phase, tokens, prompt, seconds, at, **more):
        return {
            "type": "model_step", "episode": episode, "phase": phase, "time": at,
            "usage": {
                "tokens": tokens, "prompt_tokens": prompt, "reported_tokens": tokens,
                "elapsed_seconds": seconds, "images": 4, **more,
            },
        }  # fmt: skip

    temporal = [
        {"type": "planned", "time": 100.0},
        {"type": "request_cost_calibrated", "tokens": 700, "time": 101.0},
        step(3, "coarse", 1000, 900, 4.0, 110.0),
        step(3, "refine", 500, 400, 2.0, 120.0),
        step(3, "refine-2", 500, 400, 2.0, 123.0),
        step(4, "coarse", 9999, 9000, 9.0, 130.0),
        step(3, "coarse", 50, 40, 1.0, 140.0, cached=True),
    ]
    review = [step(3, "anchor-000010", 300, 250, 1.0, 150.0)]
    use = stats.usage_of([("temporal", temporal), ("review", review)], 3, probe=True)
    assert use["requests"] == {"coarse": 1, "refine": 2, "review": 1, "probe": 2}
    assert use["model_seconds"] == {
        "coarse": 4.0, "refine": 4.0, "review": 1.0, "probe": None,
    }  # fmt: skip
    assert use["total_tokens"] == 1000 + 500 + 500 + 300 + 700
    assert use["prompt_tokens"] == 900 + 400 + 400 + 250
    assert use["completion_tokens"] == use["total_tokens"] - use["prompt_tokens"]
    assert use["images"] == 16 and use["external_tokens"] == 0
    assert use["first_request_at"] == 106.0  # 110 - 4
    # Without the batch's probes (not the first demo) and for an unknown episode.
    assert stats.usage_of([("temporal", temporal)], 3)["requests"]["probe"] == 0
    none = stats.usage_of([("temporal", temporal)], 99)
    assert none["total_tokens"] == 0 and none["first_request_at"] is None
    # Tokens the server did not report are not split.
    unreported = [{"type": "model_step", "episode": 1, "phase": "coarse",
                   "usage": {"tokens": 800, "elapsed_seconds": 1.0}}]  # fmt: skip
    got = stats.usage_of([("temporal", unreported)], 1)
    assert got["total_tokens"] == 800 and got["prompt_tokens"] is None
    assert got["completion_tokens"] is None


def test_records_rotate_and_a_damaged_file_is_read_without_crashing(tmp_path):
    folder = tmp_path / "live"
    for n in range(80):
        stats.record(folder, {"demo": f"demo_{n:04d}", "at": n}, max_bytes=3000, keep=2)
    files = sorted(p.name for p in folder.iterdir())
    assert files == ["stats.jsonl", "stats.jsonl.1", "stats.jsonl.2"]
    rows = stats.read(folder)
    assert rows and rows[-1]["demo"] == "demo_0079"
    assert [r["at"] for r in rows] == sorted(r["at"] for r in rows)  # oldest first
    assert len(rows) < 80  # the oldest rotated away: bounded
    assert [r["demo"] for r in stats.read(folder, limit=2)] == [
        "demo_0078",
        "demo_0079",
    ]
    assert all(r["at"] >= 75 for r in stats.read(folder, since=75))
    # Damage: junk, a torn last line, a non-object, another schema, an old row
    # with fields missing.
    current = folder / "stats.jsonl"
    current.write_bytes(
        current.read_bytes()
        + b"not json\n\xff\xfe\n[1]\n"
        + b'{"schema": "other.v1", "demo": "x"}\n'
        + b'{"schema": "levi.live.episode_stats.v1", "demo": "old"}\n'
        + b'{"schema": "levi.live.episode_stats.v1", "demo": "torn'
    )
    after = stats.read(folder)
    assert after[-1]["demo"] == "old" and after[-1]["timeline"]["to_mirror_s"] is None
    assert "x" not in {r["demo"] for r in after}
    assert stats.read(tmp_path / "missing") == []
    stats.record(folder / "stats.jsonl" / "nope", {"demo": "d"})  # unwritable: silent
    stats.record(folder, {"demo": object()})  # unserialisable: silent
    assert gating.HISTORY != stats.FILE


def test_a_labelled_batch_writes_one_complete_record_per_demo(env):  # noqa: F811
    e = env()
    e.rollouts.write(0)
    e.rollouts.write(1)
    e.run()
    rows = stats.read(e.ws / "live")
    assert [r["demo"] for r in rows] == ["demo_0000", "demo_0001"]
    for number, row in enumerate(rows):
        assert row["schema"] == stats.SCHEMA and row["dataset"] == NAME
        assert row["episode_index"] == number and row["excluded"] is False
        assert row["attempts"] == 0 and row["at"] > 0
        assert row["episode"]["frames"] and row["episode"]["episode_seconds"] > 0
        t = row["timeline"]
        assert all(t[k] is not None and t[k] >= -1 for k in t), t
        # mirror, then plan, then the first request, then commit and verdict.
        assert t["to_mirror_s"] <= t["to_plan_s"] <= t["to_commit_s"]
        assert t["to_first_request_s"] <= t["to_commit_s"] <= t["to_verdict_s"]
        m = row["model"]
        assert m["requests"]["coarse"] >= 1 and m["requests"]["review"] >= 1
        assert m["total_tokens"] > 0 and m["images"] > 0 and m["external_tokens"] == 0
        assert m["prompt_tokens"] + m["completion_tokens"] == m["total_tokens"]
        assert m["model_seconds"]["probe"] is None
        g = row["gate"]
        assert g["closed_wait_s"] == 0 and g["interruptions"] == 0
        assert g["vllm_wake_s"] is None and g["vllm_cold_start_s"] is None
        r = row["result"]
        assert r["state"] == "done" and r["segments"] == 5 and r["review"] == "auto"
        assert sum(r["segment_labels"].values()) == 5
        assert (
            r["verdict"]["outcome"] == "success" and r["verdict"]["valid_events"] == 1
        )
        assert r["spec"]["guideline"].startswith("generic-guideline")
        assert r["spec"]["sha256"] and r["provider"]
    # Nothing secret in the file.
    text = (e.ws / "live/stats.jsonl").read_text().lower()
    assert not {"api_key", "authorization", "bearer", "secret"} & set(text.split())
    json.loads(text.splitlines()[0])


def test_a_cold_start_and_a_wake_are_timed_for_the_next_batch(live, serve):  # noqa: F811
    c, _ = live
    vllm = gpumgr.Vllm(c)
    assert vllm.take_timings() == {}
    assert vllm.start(c.vllm_profile())
    assert wait_for(lambda: vllm.poll() == "ready")
    timings = vllm.take_timings()
    assert set(timings) == {"vllm_cold_start_s"} and timings["vllm_cold_start_s"] >= 0
    assert vllm.take_timings() == {}  # handed over once
    assert vllm.sleep() and vllm.wake()
    assert set(vllm.take_timings()) == {"vllm_wake_s"}
    vllm.stop()


def test_the_worker_reads_what_the_supervisor_timed(monkeypatch):
    monkeypatch.setenv("LEVI_LIVE_VLLM_TIMINGS", '{"vllm_wake_s": 0.74}')
    assert worker.Worker.vllm_timings() == {"vllm_wake_s": 0.74}
    monkeypatch.setenv("LEVI_LIVE_VLLM_TIMINGS", "garbage")
    assert worker.Worker.vllm_timings() == {}
    monkeypatch.delenv("LEVI_LIVE_VLLM_TIMINGS")
    assert worker.Worker.vllm_timings() == {}
