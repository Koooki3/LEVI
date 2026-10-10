"""``levi.performance``: the read-only trace view and the CPU benchmark."""

import json
import sqlite3

import pytest

from levi.live import gating, stats
from levi.performance import __main__ as cli
from levi.performance import bench, trace

T0 = 1_800_000_000.0


def record(live_dir, demo, verdict=20.0, completed=T0, **extra):
    row = {
        "at": completed + verdict,
        "dataset": "g__t",
        "demo": demo,
        "episode_index": int(demo[-1]),
        "session": "s1",
        "batch": {"id": 7, "size": 2},
        "episode": {"frames": 180},
        "timeline": {
            "to_mirror_s": 1.0,
            "to_plan_s": 2.5,
            "to_first_request_s": 3.0,
            "to_commit_s": verdict - 1,
            "to_verdict_s": verdict,
            "completed_at": completed,
        },
        "model": {
            "requests": {"coarse": 1, "refine": 1, "review": 2},
            "model_seconds": {"coarse": 8.0, "refine": 6.0, "review": 2.0},
            "prompt_tokens": 23000,
            "completion_tokens": 300,
            "images": 44,
        },
        "gate": {"closed_wait_s": 0.0, "interruptions": 0},
        "result": {"state": "committed"},
    }
    row.update(extra)
    stats.record(live_dir, row)


@pytest.fixture
def live(tmp_path):
    folder = tmp_path / "live"
    folder.mkdir()
    return folder


def test_an_episode_trace_merges_stats_and_gate(live):
    record(live, "demo_0001")
    record(live, "demo_0002", verdict=30.0, completed=T0 + 100)
    # The gate is closed for 5 s inside the first episode's 20 s.
    gating.record_transition(
        live, {"at": T0 - 10, "to": {"open": True, "code": "open"}}
    )
    gating.record_transition(
        live, {"at": T0 + 4, "to": {"open": False, "code": "policy"}}
    )
    gating.record_transition(live, {"at": T0 + 9, "to": {"open": True, "code": "open"}})
    out = trace.build(live_dir=live)
    first, second = out["episodes"]
    assert first["stages_s"]["total"] == 20.0 and first["stages_s"]["plan"] == 1.5
    assert first["model"]["seconds"] == 16.0 and first["non_model_s"] == 4.0
    assert first["model"]["requests"] == 4 and first["model"]["images"] == 44
    assert first["gate"]["window"]["closed_s"] == 5.0
    assert second["gate"]["window"]["closed_s"] == 0.0
    assert first["trace_id"] != second["trace_id"]
    assert trace.trace_id(stats.read(live)[0]) == first["trace_id"]  # stable
    assert out["summary"]["episodes"] == 2
    assert out["summary"]["stages_s"]["total"]["median"] == 25.0
    assert "decode_s" in out["unmeasured"]
    assert out["sources"]["live"] == {"stats_records": 2, "gate_transitions": 3}


def test_a_damaged_or_partial_record_gives_nulls_not_numbers(live):
    stats.record(live, {"demo": "demo_0009", "dataset": "g__t"})
    with (live / "stats.jsonl").open("a") as handle:
        handle.write("{torn\n")
    [one] = trace.build(live_dir=live)["episodes"]
    assert one["stages_s"]["total"] is None and one["non_model_s"] is None
    assert one["model"]["seconds"] is None and one["gate"]["window"] is None
    assert trace.build(live_dir=live)["summary"]["model_s"]["n"] == 0


def test_gate_history_drops_session_names_and_bad_lines(live):
    with (live / "gate.jsonl").open("w") as handle:
        handle.write(
            json.dumps(
                {
                    "at": 5,
                    "to": {"open": False, "code": "c"},
                    "sessions": [{"group": "secret-group", "task": "secret-task"}],
                }
            )
            + "\nnot json\n"
            + json.dumps({"to": {"open": True}})
            + "\n"
        )
    rows = trace.read_gate(live)
    assert rows == [{"at": 5.0, "to": {"open": False, "code": "c"}}]
    assert "secret" not in json.dumps(trace.build(live_dir=live))


def test_usage_and_cost_come_from_the_workspace_read_only(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "agent").mkdir(parents=True)
    path = workspace / "agent/workbench.sqlite3"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE records(kind TEXT, id TEXT, body TEXT NOT NULL, PRIMARY KEY(kind,id))"
    )
    sample = {"run_id": "r1", "agent_key": "model:x", "workflow": "temporal",
              "episodes": 3, "evidence_frames": 30, "tokens": 9000, "at": 5.0,
              "source": "measured"}  # fmt: skip
    db.execute(
        "INSERT INTO records VALUES('usage_samples','r1:measured',?)",
        (json.dumps(sample),),
    )
    db.execute("INSERT INTO records VALUES('runs','r1','{}')")
    db.commit()
    db.close()
    ledger = workspace / "datasets/d/tasks/r1"
    ledger.mkdir(parents=True)
    (ledger / "ledger.json").write_text(
        json.dumps(
            {
                "cost": {
                    "run_id": "r1",
                    "agent_key": "model:x",
                    "provider_kind": "api",
                    "tokens": {"value": 9000, "source": "metered"},
                }
            }
        )
    )
    before = sorted(
        (p, p.stat().st_mtime_ns, p.stat().st_size) for p in workspace.rglob("*")
    )
    out = trace.build(workspace=workspace)
    assert out["usage"][0]["tokens"] == 9000 and out["usage"][0]["run_id"] == "r1"
    assert (
        out["cost"][0]["tokens"] == 9000 and out["cost"][0]["token_source"] == "metered"
    )
    assert (
        sorted(
            (p, p.stat().st_mtime_ns, p.stat().st_size) for p in workspace.rglob("*")
        )
        == before
    )
    assert trace.build(workspace=tmp_path / "none")["usage"] == []


def test_the_trace_command_prints_json_and_stores_nothing(live, tmp_path, capsys):
    record(live, "demo_0001")
    before = sorted(p.name for p in live.rglob("*"))
    assert cli.main(["trace", "--live-dir", str(live), "--summary-only"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["schema"] == trace.SCHEMA and "episodes" not in printed
    assert sorted(p.name for p in live.rglob("*")) == before


def test_the_benchmark_reports_numbers_and_removes_its_scratch(tmp_path):
    out = bench.run(frames=30, size="160x120", repeat=2, scratch=tmp_path / "s")
    assert out["schema"] == bench.SCHEMA
    assert out["cases"]["pts"]["identical"] is True
    for case in (
        out["cases"]["hash"],
        out["cases"]["pts"]["scans"]["packet"],
        out["cases"]["pixels"]["methods"]["float"],
    ):
        assert (
            case["runs"] == 2
            and case["median_s"] > 0
            and case["p95_s"] >= case["median_s"]
        )
    assert out["params"]["frames"] == 30 and out["machine"]["cpu_count"]
    assert list((tmp_path / "s").iterdir()) == []
    with pytest.raises(ValueError):
        bench.run(["nope"])
