"""``live/stats.jsonl``: one structured record per labelled demo."""

import json
import time

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
            "attempts", "excluded", "backfilled", "batch.id", "batch.size",
            "episode.frames", "episode.episode_seconds",
            "timeline.to_mirror_s", "timeline.to_plan_s",
            "timeline.to_first_request_s", "timeline.to_commit_s",
            "timeline.to_verdict_s", "timeline.completed_at",
            "timeline.first_request_at",
            "model.requests.coarse", "model.requests.refine",
            "model.requests.review", "model.requests.probe",
            "model.model_seconds.coarse", "model.model_seconds.refine",
            "model.model_seconds.review", "model.model_seconds.probe",
            "model.tokens.coarse", "model.tokens.refine",
            "model.tokens.review", "model.tokens.probe",
            "model.prompt_tokens", "model.completion_tokens",
            "model.total_tokens", "model.probe_tokens", "model.reserved_tokens",
            "model.unreported_steps", "model.images", "model.external_tokens",
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
    # The calibration probe is its own number, not part of total or completion.
    assert use["total_tokens"] == 1000 + 500 + 500 + 300
    assert use["probe_tokens"] == 700 and use["reserved_tokens"] is None
    assert use["unreported_steps"] == 0
    assert use["prompt_tokens"] == 900 + 400 + 400 + 250
    assert use["completion_tokens"] == (1000 - 900) + 100 + 100 + 50
    assert use["total_tokens"] == use["prompt_tokens"] + use["completion_tokens"]
    assert use["images"] == 16 and use["external_tokens"] == 0
    assert use["first_request_at"] == 106.0  # 110 - 4
    # Without the batch's probes (not the first demo) and for an unknown episode.
    assert stats.usage_of([("temporal", temporal)], 3)["requests"]["probe"] == 0
    none = stats.usage_of([("temporal", temporal)], 99)
    assert none["total_tokens"] == 0 and none["first_request_at"] is None
    assert stats.usage_of([("temporal", temporal)], 3)["probe_tokens"] is None
    # A step the server reported no usage for carries a reservation, which is
    # not spent tokens: it is kept apart and out of total and completion.
    unreported = [{"type": "model_step", "episode": 1, "phase": "coarse",
                   "usage": {"tokens": 800, "elapsed_seconds": 1.0}}]  # fmt: skip
    got = stats.usage_of([("temporal", unreported)], 1)
    assert got["total_tokens"] == 0 and got["prompt_tokens"] is None
    assert got["completion_tokens"] is None
    assert got["reserved_tokens"] == 800 and got["unreported_steps"] == 1


def test_a_probe_a_reported_step_and_a_reserved_step_stay_apart():
    """The reviewer's case: probe 5000; one step prompt 1000, total 1200; one
    step with no usage from the server, reserved 8192."""
    journal = [
        {"type": "request_cost_calibrated", "tokens": 5000, "time": 1.0},
        {"type": "model_step", "episode": 7, "phase": "coarse", "time": 5.0,
         "usage": {"tokens": 1200, "prompt_tokens": 1000, "reported_tokens": 1200,
                   "elapsed_seconds": 2.0}},
        {"type": "model_step", "episode": 7, "phase": "refine", "time": 9.0,
         "usage": {"tokens": 8192, "prompt_tokens": None, "reported_tokens": None,
                   "elapsed_seconds": 2.0}},
    ]  # fmt: skip
    use = stats.usage_of([("temporal", journal)], 7, probe=True)
    assert use["completion_tokens"] == 200 and use["prompt_tokens"] == 1000
    assert use["total_tokens"] == 1200
    assert use["probe_tokens"] == 5000 and use["reserved_tokens"] == 8192
    assert use["unreported_steps"] == 1
    # An older record without the new fields reads them as null.
    old = stats.normalize({"model": {"total_tokens": 3}})["model"]
    assert old["probe_tokens"] is None and old["reserved_tokens"] is None


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
        kinds = m["tokens"]
        assert kinds["coarse"] + kinds["refine"] + kinds["review"] == m["total_tokens"]
        assert kinds["probe"] == m["probe_tokens"]
        assert m["tokens"]["coarse"] > 0 and m["tokens"]["review"] > 0
        # Absolute moments (later additions to the schema) and the batch.
        assert t["completed_at"] > 1e9 and t["first_request_at"] > t["completed_at"]
        assert row["batch"] == {"id": rows[0]["batch"]["id"], "size": 2}
        assert row["batch"]["id"] > 1e9
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


def test_a_cold_start_and_a_wake_are_timed_until_a_batch_finishes(live, serve):  # noqa: F811
    c, _ = live
    vllm = gpumgr.Vllm(c)
    now = time.time
    assert vllm.pending_timings(now(), 600) == {}
    assert vllm.start(c.vllm_profile())
    assert wait_for(lambda: vllm.poll() == "ready")
    given = vllm.pending_timings(now(), 600)
    assert set(given) == {"vllm_cold_start_s"} and given["vllm_cold_start_s"]["s"] >= 0
    # Reading is not consuming: a batch that did not finish leaves it.
    assert set(vllm.pending_timings(now(), 600)) == {"vllm_cold_start_s"}
    assert vllm.sleep() and vllm.wake()
    assert set(vllm.pending_timings(now(), 600)) == {
        "vllm_cold_start_s",
        "vllm_wake_s",
    }
    # Clearing drops what was handed over, not a wake measured since.
    vllm.clear_timings(given)
    assert set(vllm.timings) == {"vllm_wake_s"}
    # Stale ones are dropped.
    assert vllm.pending_timings(now() + 601, 600) == {}
    vllm.stop()


class FakeProcess:
    pid = 4242

    def __init__(self, *args, **kwargs):
        self.env = kwargs.get("env")
        self.code = None

    def poll(self):
        return self.code


def spawned_with(fixture):
    from levi.live import controller

    c, _ = fixture
    made = []

    def popen(*args, **kwargs):
        made.append(FakeProcess(*args, **kwargs))
        return made[-1]

    ctl = controller.Controller(c, log=lambda *a: None, popen=popen)
    return ctl, made


def test_timings_survive_a_worker_that_does_not_finish_a_batch(live, serve):  # noqa: F811
    ctl, made = spawned_with(live)
    ctl.vllm.timings["vllm_wake_s"] = {"s": 0.74, "at": time.time()}
    for code in (14, 10, 11, 13, 12):  # nothing to do, model, person, paused, error
        ctl._spawn("ds")
        assert json.loads(made[-1].env["LEVI_LIVE_VLLM_TIMINGS"]) == {
            "vllm_wake_s": 0.74
        }
        ctl._reaped(code)
        assert "vllm_wake_s" in ctl.vllm.timings, code
    # The batch that finishes takes them; the next one gets none.
    ctl._spawn("ds")
    ctl._reaped(0)
    assert ctl.vllm.timings == {}
    ctl._spawn("ds")
    assert "LEVI_LIVE_VLLM_TIMINGS" not in made[-1].env
    ctl._reaped(14)


def test_a_wake_that_is_hours_old_is_not_given_to_a_later_batch(live, serve):  # noqa: F811
    ctl, made = spawned_with(live)
    ctl.vllm.timings["vllm_cold_start_s"] = {"s": 55.0, "at": time.time() - 3 * 3600}
    ctl._spawn("ds")
    assert "LEVI_LIVE_VLLM_TIMINGS" not in made[-1].env
    assert ctl.vllm.timings == {}
    ctl._reaped(14)


def test_the_worker_reads_what_the_supervisor_timed(monkeypatch):
    monkeypatch.setenv("LEVI_LIVE_VLLM_TIMINGS", '{"vllm_wake_s": 0.74}')
    assert worker.Worker.vllm_timings() == {"vllm_wake_s": 0.74}
    monkeypatch.setenv("LEVI_LIVE_VLLM_TIMINGS", "garbage")
    assert worker.Worker.vllm_timings() == {}
    monkeypatch.delenv("LEVI_LIVE_VLLM_TIMINGS")
    assert worker.Worker.vllm_timings() == {}
