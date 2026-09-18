"""Format registry contracts, single-pass equivalence and RECAP conformance.

Adding a format means adding a fixture builder to FIXTURES; every test that
is parametrized over the registry then covers it.
"""

import csv
import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from levi.conversion import media, registry
from levi.conversion.dataset import validate
from levi.conversion.engine import execute
from levi.conversion.options import Options


def _video(path: Path, n: int, fps: float, size="64x48"):
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={size}:rate={fps}:duration={n / fps}",
            "-frames:v",
            str(n),
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )


def make_demo(demo: Path, n=30, fps=10.0, outcome=None, images=False, stale=0):
    """One capture demo. ``outcome`` makes it a policy rollout."""
    demo.mkdir(parents=True)
    t = np.arange(n) / fps + 1.7e9
    source = t.copy()
    if stale:
        source[5 : 5 + stale + 1] = source[5]
    ids = np.arange(1, n + 1)  # real captures start at 1
    flag = int(outcome == "success")
    motion = np.where(np.arange(n) % 3 == 0, np.arange(n), np.arange(n) - 1) * 0.004
    pd.DataFrame(
        {
            "timestamp_sec": t,
            "frame_index": ids,
            "success_flag": flag,
            "source_stamp_sec": source,
            "px": motion,
            "py": 0.0,
            "pz": 0.0,
            "qx": 0.0,
            "qy": 0.0,
            "qz": 0.0,
            "qw": 1.0,
        }
    ).to_csv(demo / "end_effector_pose.csv", index=False)
    pd.DataFrame(
        {
            "timestamp_sec": t,
            "frame_index": ids,
            "success_flag": flag,
            "source_stamp_sec": source,
            "finger_left": 0.01,
            "finger_right": 0.01,
            "gripper_width": 0.02,
            "last_gripper_command": ["open"] * (n // 2) + ["close"] * (n - n // 2),
        }
    ).to_csv(demo / "gripper_state.csv", index=False)
    (demo / "events.csv").write_text(
        "timestamp_sec,frame_index,event\n"
        f"{t[0]},1,start_demo\n{t[n // 2]},{n // 2 + 1},gripper\n"
        f"{t[-1]},{n},{'episode_end' if outcome else 'stop_demo'}\n"
    )
    meta = {"frame_count": n, "stopped_at": "2026-09-18T00:00:00"}
    if outcome:
        meta.update(
            data_source="policy_rollout",
            success_flag_final=flag,
            eval={"outcome": outcome},
        )
    (demo / "metadata.json").write_text(json.dumps(meta))
    for camera in Options().cameras:
        if images:
            import cv2

            tmp = demo / f"{camera}.tmp.mp4"
            _video(tmp, n, fps)
            (demo / camera).mkdir()
            for i, frame in enumerate(media.decode(tmp)):
                cv2.imwrite(str(demo / camera / f"frame_{i}.png"), frame)
            tmp.unlink()
        else:
            _video(demo / f"{camera}.mp4", n, fps)


def capture_fixture(root: Path, outcomes=("success", "failure"), fps=(10.0, 10.0)):
    task = root / "pick_screws"
    for i, (outcome, rate) in enumerate(zip(outcomes, fps)):
        make_demo(task / f"demo_{i:04d}", n=30 + 4 * i, fps=rate, outcome=outcome)
    (task / "task_description.txt").write_text("Pick the screw")
    return root


def image_fixture(root: Path):
    task = root / "task"
    make_demo(task / "demo_0000", n=20, images=True, outcome="success")
    (task / "task_description.txt").write_text("Move")
    return root


def lerobot_fixture(root: Path):
    source = capture_fixture(root.parent / (root.name + "_raw"))
    execute("pipeline", source, root, Options(timing="retime", filter_static=False))
    return root


FIXTURES = {
    "robot_capture": capture_fixture,
    "image_sequence": image_fixture,
    "lerobot": lerobot_fixture,
}


def hashes(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_registry_covers_every_fixture():
    assert set(FIXTURES) == set(registry.INPUT_FORMATS)


@pytest.mark.parametrize("fmt", list(registry.INPUT_FORMATS))
def test_detect_own_fixture_best(fmt, tmp_path):
    root = FIXTURES[fmt](tmp_path / fmt)
    assert registry.detect(root).id == fmt


@pytest.mark.parametrize("fmt", list(registry.INPUT_FORMATS))
def test_inspect_report_is_complete(fmt, tmp_path):
    root = FIXTURES[fmt](tmp_path / fmt)
    report = registry.inspect(root, Options())
    assert report.format == fmt and report.requirements
    assert not report.failed, report.failed
    assert {t.target for t in report.targets} == set(registry.OUTPUT_FORMATS)
    for item in report.requirements:
        assert item.label and item.status in ("pass", "warn", "fail", "info")
        # Decode-only checks are never reported as already passed.
        if item.verified == "during_scan":
            assert item.status == "info" and item.fix
    json.dumps(report.model_dump())  # the API returns it verbatim


@pytest.mark.parametrize(
    ("fmt", "target"),
    [
        (i, o.id)
        for i in registry.INPUT_FORMATS
        for o in registry.OUTPUT_FORMATS.values()
        if i in o.inputs
    ],
)
def test_round_trip_every_compatible_pair(fmt, target, tmp_path):
    root = FIXTURES[fmt](tmp_path / fmt)
    before = hashes(root)
    options = registry.with_defaults(Options(), target)
    result = execute("pipeline", root, tmp_path / "out", options)
    out = Path(result["dataset_path"])
    assert result["ok"] and out == (tmp_path / "out").resolve()
    assert validate(out)["ok"]
    assert (out / "meta/levi_validation.json").is_file()
    assert not list(tmp_path.glob(".*.partial")), "staging must be renamed or removed"
    assert hashes(root) == before, "sources are never modified"


def test_retime_remuxes_losslessly(tmp_path):
    root = capture_fixture(tmp_path / "cap", fps=(9.5, 9.5))
    result = execute(
        "pipeline",
        root,
        tmp_path / "out",
        Options(timing="retime", filter_static=False, fps=10),
    )
    assert result["video_modes"] == {"remux": 4}
    out = Path(result["dataset_path"])
    src = root / "pick_screws/demo_0000/wrist_camera.mp4"
    dst = next((out / "videos").rglob("observation.images.hand/episode_000000.mp4"))
    decoded = lambda p: [hashlib.sha1(f.tobytes()).hexdigest() for f in media.decode(p)]
    assert decoded(src) == decoded(dst)
    times = np.array(media.packet_times(dst))
    assert np.allclose(times - times[0], np.arange(len(times)) / 10, atol=1e-3)
    assert json.loads((out / "meta/info.json").read_text())["fps"] == 10


def test_single_pass_matches_the_legacy_stage_chain(tmp_path):
    """New pipeline == fps resample → static filter → convert, stage by stage
    (parquet columns, lengths, provenance); pixels differ only by encodes."""
    root = tmp_path / "cap"
    task = root / "task"
    make_demo(task / "demo_0000", n=40, fps=9.5, stale=1)
    make_demo(task / "demo_0001", n=33, fps=9.5)
    (task / "task_description.txt").write_text("slug_task")
    opts = Options(fps=9.5, task_map={"slug_task": "Pick it"})
    new = Path(execute("pipeline", root, tmp_path / "new", opts)["dataset_path"])

    execute("fps", root, tmp_path / "s1", opts)
    execute("filter", tmp_path / "s1", tmp_path / "s2", opts)
    # The staged copy already carries the mapped task text.
    old = Path(
        execute(
            "convert",
            tmp_path / "s2",
            tmp_path / "old",
            opts.model_copy(update={"task_map": {}}),
        )["dataset_path"]
    )
    for ep in range(2):
        a = pd.read_parquet(new / f"data/chunk-000/episode_{ep:06d}.parquet")
        b = pd.read_parquet(old / f"data/chunk-000/episode_{ep:06d}.parquet")
        assert list(a.columns) == list(b.columns)
        for column in a.columns:
            assert np.allclose(np.stack(a[column]), np.stack(b[column])), column
    read = lambda p: [
        json.loads(x)
        for x in (p / "meta/levi_provenance.jsonl").read_text().splitlines()
    ]
    for x, y in zip(read(new), read(old)):
        assert x["source_frame_ids"] == y["source_frame_ids"]
        assert x["source_demo"] == y["source_demo"]
        assert x["source_positions"][0] == 0
    assert "Pick it" in (new / "meta/tasks.jsonl").read_text()


def test_task_map_is_applied_once_not_chained(tmp_path):
    root = image_fixture(tmp_path / "cap")
    opts = Options(source_fps=10, fps=10, task_map={"Move": "b", "b": "c"})
    out = Path(execute("pipeline", root, tmp_path / "out", opts)["dataset_path"])
    assert json.loads((out / "meta/tasks.jsonl").read_text())["task"] == "b"


def test_failed_preflight_publishes_nothing(tmp_path):
    root = capture_fixture(tmp_path / "cap")
    demo = root / "pick_screws/demo_0001"
    pose = pd.read_csv(demo / "end_effector_pose.csv")
    pose["source_stamp_sec"] = pose["source_stamp_sec"].iloc[0]  # frozen state
    pose.to_csv(demo / "end_effector_pose.csv", index=False)
    with pytest.raises(ValueError, match="preflight"):
        execute("pipeline", root, tmp_path / "out", Options())
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".out.partial"))


def test_progress_file_reports_stages(tmp_path):
    root = capture_fixture(tmp_path / "cap")
    progress = tmp_path / "p.json"
    execute("pipeline", root, tmp_path / "out", Options(), progress)
    state = json.loads(progress.read_text())
    assert state["stage"] == "done" and state["stage_index"] == len(state["stages"])
    assert state["stages"][:3] == ["Inspect", "Plan", "Convert episodes"]


# --- RECAP -------------------------------------------------------------------


def rlinf_compute_returns_for_episode(
    episode_length, is_success, gamma, failure_reward
):
    # Verbatim from RLinf examples/offline_rl/advantage_labeling/recap/process/
    # compute_returns.py @ db66ac56 (Apache-2.0) — the consumer we must match.
    rewards = np.full(episode_length, -1.0, dtype=np.float32)
    rewards[-1] = 0.0 if is_success else failure_reward

    returns = np.zeros(episode_length, dtype=np.float32)
    returns[-1] = rewards[-1]
    for t in range(episode_length - 2, -1, -1):
        returns[t] = rewards[t] + gamma * returns[t + 1]

    return returns, rewards


def rlinf_sidecar(root: Path, dataset_type="rollout", gamma=1.0, failure_reward=-300.0):
    """RLinf's _process_single_parquet, reduced to the columns it reads."""
    tasks = {
        json.loads(x)["task_index"]: json.loads(x)["task"]
        for x in (root / "meta/tasks.jsonl").read_text().splitlines()
    }
    rows = []
    for path in sorted((root / "data").rglob("*.parquet")):
        table = pq.read_table(
            path, columns=["episode_index", "frame_index", "is_success", "task_index"]
        )
        is_success = table.column("is_success").to_pylist()
        n = table.num_rows
        success = True if dataset_type == "sft" else bool(is_success[n - 1])
        ret, rew = rlinf_compute_returns_for_episode(n, success, gamma, failure_reward)
        for i in range(n):
            rows.append(
                (
                    table.column("episode_index")[i].as_py(),
                    table.column("frame_index")[i].as_py(),
                    float(ret[i]),
                    float(rew[i]),
                    tasks[table.column("task_index")[i].as_py()],
                )
            )
    return rows


def _sidecar(root: Path, name="returns.parquet"):
    table = pq.read_table(root / "meta" / name)
    assert table.schema.names == [
        "episode_index",
        "frame_index",
        "return",
        "reward",
        "prompt",
    ]
    assert str(table.schema.field("return").type) == "float"
    assert str(table.schema.field("episode_index").type) == "int64"
    frame = table.to_pandas()
    return [tuple(r) for r in frame.itertuples(index=False)]


def recap_options(**target):
    return registry.with_defaults(Options(target_options=target), "recap_value")


def test_recap_export_matches_rlinf_compute_returns(tmp_path):
    root = capture_fixture(tmp_path / "cap")
    out = Path(
        execute("pipeline", root, tmp_path / "recap", recap_options())["dataset_path"]
    )
    assert _sidecar(out) == rlinf_sidecar(out)
    info = json.loads((out / "meta/info.json").read_text())
    assert info["features"]["is_success"]["dtype"] == "bool"
    stats = json.loads((out / "meta/stats.json").read_text())
    assert stats["return"]["min"] == pytest.approx(-300 - 33)
    assert stats["return"]["max"] == 0  # a success's terminal step
    labels = list(csv.DictReader((out / "meta/episode_labels.csv").open()))
    assert [(r["episode_index"], r["success"]) for r in labels] == [
        ("0", "1"),
        ("1", "0"),
    ]
    manifest = json.loads((out / "meta/levi_recap.json").read_text())
    assert manifest["outcomes"] == {"success": 1, "failure": 1}
    assert manifest["task_max_episode_length"] == {"Pick the screw": 34}
    # Every executed step kept: one row per CSV row.
    assert info["total_frames"] == 30 + 34
    frame = pd.read_parquet(out / "data/chunk-000/episode_000000.parquet")
    assert frame.is_success.tolist() == [False] * 29 + [True]
    assert frame["next.done"].tolist() == [False] * 29 + [True]


def test_recap_from_existing_lerobot_dataset_equals_raw_export(tmp_path):
    raw_root = capture_fixture(tmp_path / "cap")
    direct = Path(
        execute("pipeline", raw_root, tmp_path / "direct", recap_options(tag="v1"))[
            "dataset_path"
        ]
    )
    lerobot = Path(
        execute(
            "pipeline",
            raw_root,
            tmp_path / "lr",
            Options(timing="retime", filter_static=False),
        )["dataset_path"]
    )
    derived = Path(
        execute("pipeline", lerobot, tmp_path / "derived", recap_options(tag="v1"))[
            "dataset_path"
        ]
    )
    assert _sidecar(direct, "returns_v1.parquet") == _sidecar(
        derived, "returns_v1.parquet"
    )
    assert _sidecar(derived, "returns_v1.parquet") == rlinf_sidecar(derived)
    assert validate(derived)["ok"]
    # Videos reused, not re-encoded.
    a = next((lerobot / "videos").rglob("*.mp4"))
    b = derived / a.relative_to(lerobot)
    assert a.read_bytes() == b.read_bytes()


def test_recap_human_labels_and_exclusions_from_lerobot(tmp_path):
    lerobot = lerobot_fixture(tmp_path / "lr")
    options = recap_options().model_copy(
        update={"outcome_labels": {"1": "success"}, "exclude_demos": ["0"]}
    )
    out = Path(execute("pipeline", lerobot, tmp_path / "out", options)["dataset_path"])
    rows = [
        json.loads(x) for x in (out / "meta/episodes.jsonl").read_text().splitlines()
    ]
    assert rows == [
        {
            **rows[0],
            "episode_index": 0,
            "levi_outcome": "success",
            "levi_outcome_source": "human",
        }
    ]
    assert validate(out)["ok"]


def test_teleop_capture_is_unsupported_for_recap_with_solutions(tmp_path):
    root = tmp_path / "teleop"
    make_demo(root / "task/demo_0000")
    make_demo(root / "task/demo_0001")
    (root / "task/task_description.txt").write_text("Pick")
    (root / "task/task_description.txt").write_text("Pick")
    report = registry.inspect(root, Options())
    targets = {t.target: t for t in report.targets}
    assert targets["lerobot_v21"].status == "supported"
    recap = targets["recap_value"]
    assert recap.status == "unsupported" and "label" in recap.reasons[0]
    assert {s.id for s in recap.solutions} == {"label_outcomes", "export_sft"}
    assert recap.defaults == {"timing": "retime", "filter_static": False}
    # Picking the SFT solution makes it exportable.
    sft = next(s for s in recap.solutions if s.id == "export_sft")
    options = recap_options(**sft.options["target_options"])
    out = Path(execute("pipeline", root, tmp_path / "out", options)["dataset_path"])
    assert _sidecar(out) == rlinf_sidecar(out, "sft")


def test_partially_labelled_rollout_offers_exclusion(tmp_path):
    root = capture_fixture(tmp_path / "cap", outcomes=("success", None), fps=(10, 10))
    recap = {t.target: t for t in registry.inspect(root, Options()).targets}[
        "recap_value"
    ]
    exclude = next(s for s in recap.solutions if s.id == "exclude_unlabeled")
    assert exclude.options["exclude_demos"] == ["pick_screws/demo_0001"]


def test_lerobot_v3_input_is_explained_not_unknown(tmp_path):
    root = tmp_path / "v3"
    (root / "meta").mkdir(parents=True)
    (root / "meta/info.json").write_text(json.dumps({"codebase_version": "v3.0"}))
    report = registry.inspect(root, Options())
    assert report.format == "lerobot" and report.failed[0].id == "version"
    assert all(t.status == "unsupported" for t in report.targets)


def test_formats_and_inspect_api(client, tmp_path):
    import time

    formats = client.get("/api/levi/convert/formats").json()
    assert {o["id"] for o in formats["outputs"]} == {"lerobot_v21", "recap_value"}
    assert "recap_value" in formats["matrix"]["lerobot"]
    root = capture_fixture(tmp_path / "cap")
    job = client.post("/api/levi/convert/inspect", json={"source": str(root)}).json()
    for _ in range(200):
        item = next(
            j for j in client.get("/api/levi/jobs").json() if j["id"] == job["id"]
        )
        if item["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert item["status"] == "succeeded", item
    report = item["result"]["report"]
    assert report["variant"] == "policy_rollout"
    assert report["summary"]["outcomes"] == {"success": 1, "failure": 1}
    assert item["progress"]["stages"] == ["Inspect"]
    # A RECAP plan gets the target's defaults and the "recap" name.
    plan = client.post(
        "/api/levi/jobs/plan",
        json={
            "stage": "pipeline",
            "source": str(root),
            "options": {"target": "recap_value"},
        },
    ).json()
    assert (
        plan["options"]["timing"] == "retime" and not plan["options"]["filter_static"]
    )
    assert Path(plan["output"]).name.startswith("cap_recap_")
    bad = client.post(
        "/api/levi/jobs/plan",
        json={
            "stage": "pipeline",
            "source": str(root),
            "options": {"target": "recap_value", "target_options": {"gamma": 2}},
        },
    )
    assert bad.status_code in (400, 422)


def test_human_outcome_labels_api_export_and_plan_snapshot(client, tmp_path):
    lerobot = lerobot_fixture(tmp_path / "lr")
    repo = client.post("/api/levi/catalog", json={"path": str(lerobot)}).json()["id"]
    url = "/annotations/api/episodes/{}/outcome"
    body = {"repo_id": repo, "outcome": "success"}
    assert client.post(url.format(1), json=body).status_code == 200
    assert client.post(url.format(99), json=body).status_code == 404
    labels = client.get("/annotations/api/episodes/outcomes", params={"repo_id": repo})
    assert labels.json()["labels"]["1"]["outcome"] == "success"
    assert labels.json()["labels"]["1"]["source"] == "human"

    # The plan snapshots the label; the RECAP export applies it.
    plan = client.post(
        "/api/levi/jobs/plan",
        json={
            "stage": "pipeline",
            "source": str(lerobot),
            "options": {"target": "recap_value"},
        },
    ).json()
    assert plan["options"]["outcome_labels"] == {"1": "success"}

    # The annotated export carries it as dataset metadata.
    out = client.post("/annotations/api/export", json={"repo_id": repo}).json()
    rows = [
        json.loads(x)
        for x in (Path(out["output_dir"]) / "meta/episodes.jsonl")
        .read_text()
        .splitlines()
    ]
    assert rows[1]["levi_outcome"] == "success"
    assert rows[1]["levi_outcome_source"] == "human"
    assert rows[0]["levi_outcome"] == "success" and "levi_outcome_source" not in rows[0]

    # Clearing restores the metadata outcome.
    client.post(url.format(1), json={"repo_id": repo, "outcome": None})
    labels = client.get("/annotations/api/episodes/outcomes", params={"repo_id": repo})
    assert labels.json()["labels"] == {}


def test_conversion_docs_list_every_registered_format():
    text = (Path(__file__).resolve().parents[1] / "docs/CONVERSION.md").read_text()
    for fmt in [*registry.INPUT_FORMATS, *registry.OUTPUT_FORMATS]:
        assert f"`{fmt}`" in text, fmt


def test_missing_completion_markers_are_not_codec_warnings(tmp_path):
    root = capture_fixture(tmp_path / "cap")
    for demo in root.rglob("demo_*"):
        (demo / "events.csv").write_text("timestamp_sec,frame_index,event\n0,1,start\n")
    reqs = {r.id: r for r in registry.inspect(root, Options()).requirements}
    assert reqs["camera_codec"].status == "pass"
    assert reqs["completion"].status == "warn"
    strict = registry.inspect(root, Options(require_complete=True))
    assert {r.id for r in strict.failed} == {"completion"}


def test_cli_applies_target_defaults_unless_set(tmp_path, monkeypatch):
    import sys

    from levi.conversion import __main__ as cli

    root = capture_fixture(tmp_path / "cap")
    config = tmp_path / "recap.json"
    config.write_text(json.dumps({"target": "recap_value"}))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "levi",
            "pipeline",
            "--source",
            str(root),
            "--output",
            str(tmp_path / "out"),
            "--options",
            str(config),
        ],
    )
    cli.main()
    conversion = json.loads((tmp_path / "out/meta/levi_conversion.json").read_text())
    assert conversion["timing"] == "retime"
    assert conversion["options"]["filter_static"] is False
    assert (tmp_path / "out/meta/returns.parquet").is_file()
