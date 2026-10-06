"""Reset export: reversed episodes derived from forward ones."""

import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest
from reset_helpers import (
    OPEN_DONE,
    RELEASE,
    REST_LAST,
    N,
    make_demo,
    marker,
    wrist,
)
from test_pool import declare_grippers

from levi.conversion import media
from levi.pool import export, jobs, scanner
from levi.pool.recipe import Recipe
from levi.pool.reset import contract, events, vision
from levi.pool.reset.schema import ResetOptions

# ------------------------------------------------------------------ units


def test_reversed_actions_are_next_state_of_the_reversed_states():
    forward = np.array([[0.0], [1.0], [3.0], [6.0]], dtype=np.float32)
    action = np.vstack([forward[1:], forward[-1:]])  # [1, 3, 6, 6]
    reversed_states = contract.reversed_states(forward, [0, 1, 2, 3], {}, 0)
    assert reversed_states[:, 0].tolist() == [6, 3, 1, 0]
    # Reversing the action column puts every action one step off ...
    assert action[::-1, 0].tolist() == [6, 6, 3, 1]
    # ... the rebuilt one is the next reversed state, the last one held.
    assert contract.reversed_actions(reversed_states, "next_state")[:, 0].tolist() == [
        3,
        1,
        0,
        0,
    ]


def _state(rows):
    return np.array([[*r, 0, 0, 0, g] for r, g in rows], dtype=np.float32)


def test_contract_check_reads_the_meaning_from_the_data():
    c = contract.resolve("fr3-robotiq@1")
    state = _state([((i, 0, 0), 1.0) for i in range(6)])
    action = np.vstack([state[1:], state[-1:]])
    contract.check(c, state, action)
    with pytest.raises(contract.ContractProblem, match="meaning is unknown"):
        contract.check(c, state, action + 1.0)
    with pytest.raises(contract.ContractProblem, match="dimensions"):
        contract.check(c, state[:, :5], action[:, :5])
    odd = state.copy()
    odd[:, 6] = 0.5  # a continuous gripper is not the contract's command
    with pytest.raises(contract.ContractProblem, match="gripper"):
        contract.check(c, odd, np.vstack([odd[1:], odd[-1:]]))


def test_a_reversed_command_leads_the_finger_motion():
    # Forward: closed at row 3, open from row 6; the fingers are open at row 9.
    opened = np.array([1, 1, 1, 0, 0, 0, 1, 1, 1, 1, 1], dtype=float)
    width = np.array(
        [0.084, 0.084, 0.084, 0.07, 0.03, 0.03, 0.03, 0.06, 0.084, 0.084, 0.084]
    )
    found = events.find_events(opened, width, 3)
    grasp, release = found
    assert (grasp.kind, grasp.row, grasp.motion_end) == ("grasp", 3, 4)
    assert (release.kind, release.row, release.motion_end) == ("release", 6, 8)
    edits = events.lead_edits(found, 0.0, 1.0)
    # The release's finger motion (rows 6, 7) is reversed first, so the command
    # is already "closed" there; the grasp's (row 3) already "open".
    assert edits == {3: 1.0, 6: 0.0, 7: 0.0}
    state = np.zeros((11, 7), dtype=np.float32)
    state[:, 6] = opened
    rev = contract.reversed_states(state, list(range(11)), edits, 6)
    act = contract.reversed_actions(rev, "next_state")
    # At the reversed row of source row 8 (fingers fully open) the action says close.
    assert rev[2, 6] == 1.0 and act[2, 6] == 0.0


def test_the_wrist_view_tells_where_the_object_went():
    seen = {}
    for scenario in ("in_place", "in_reach", "escaped"):
        m = vision.measure(wrist(RELEASE - 1, scenario), wrist(REST_LAST, scenario))
        seen[scenario] = vision.classify(m)[0]
    assert seen == {
        "in_place": "in_place",
        "in_reach": "in_reach",
        "escaped": "escaped",
    }


def test_a_frame_store_reads_in_any_order_with_repeats(tmp_path):
    from reset_helpers import side, write_video

    write_video(tmp_path / "v.mp4", [side(i) for i in range(12)])
    with media.FrameStore(tmp_path / "v.mp4", tmp_path / "scratch") as store:
        assert len(store) == 12
        got = [marker(f) for f in media.ordered_frames([(store, [5, 4, 3, 3, 11, 0])])]
        # The set-based selector gives 3, 4, 5 for the same request.
        old = [marker(f) for f in media.selected_video(tmp_path / "v.mp4", [5, 4, 3])]
        with pytest.raises(IndexError):
            store[12]
    assert got == [5, 4, 3, 3, 11, 0] and old == [3, 4, 5]
    assert not list((tmp_path / "scratch").glob("*.raw"))


def test_options_are_checked():
    with pytest.raises(ValueError, match="exactly once"):
        ResetOptions(direction="reset_only", task_template="Reset")
    with pytest.raises(ValueError, match="lerobot_v21"):
        export.ExportOptions(
            format="raw_capture", name="x", reset={"direction": "reset_only"}
        )
    assert ResetOptions().enabled is False
    o = export.ExportOptions(
        format="lerobot_v21", name="x", reset={"direction": "forward_and_reset"}
    )
    # The static filter would drop the rows in which an object settles.
    assert o.filter_static is False
    plain = export.ExportOptions(format="lerobot_v21", name="y")
    assert plain.filter_static is None and plain.reset is None


# ------------------------------------------------------------------ exports

TASKS = {
    "in_place": "place a in b",
    "in_reach": "drop c into d",
    "escaped": "toss e onto f",
    "leaves": "put g near h",
    "failed": "lift i over j",
}


@pytest.fixture
def rpool(tmp_path, monkeypatch):
    root = tmp_path / "pool"
    base = root / "orig/models/pi"
    make_demo(base / "t_in_place/demo_0000", "in_place", task=TASKS["in_place"], seed=0)
    make_demo(base / "t_in_reach/demo_0000", "in_reach", task=TASKS["in_reach"], seed=1)
    make_demo(base / "t_escaped/demo_0000", "escaped", task=TASKS["escaped"], seed=2)
    make_demo(
        base / "t_leaves/demo_0000",
        "in_place",
        arm_leaves=True,
        task=TASKS["leaves"],
        seed=3,
    )
    make_demo(
        base / "t_failed/demo_0000",
        "in_place",
        task=TASKS["failed"],
        rollout="failure",
        seed=4,
    )
    monkeypatch.setenv("LEVI_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("LEVI_POOL_ROOTS", str(root))
    monkeypatch.setenv("LEVI_POOL_HELDOUT", "none")
    monkeypatch.setenv("LEVI_EXPORT_ROOTS", str(tmp_path))
    monkeypatch.setattr(export, "_levi_commit", lambda: "test")
    declare_grippers(root)
    scanner.scan()
    return {"root": root, "out": tmp_path / "exports", "tmp": tmp_path}


def run_export(rpool, tasks, name, direction="forward_and_reset", **reset):
    rec = Recipe(name="r", categories=["rollout"], tasks=[TASKS[t] for t in tasks])
    options = export.ExportOptions(
        format="lerobot_v21",
        name=name,
        output_dir=str(rpool["out"]),
        reset={"direction": direction, **reset},
    )
    job = jobs.plan_export(rec, options)
    result = jobs.execute(job)
    out = rpool["out"] / name
    return job, result, out, json.loads((out / "pool_export.json").read_text())


def read_episode(out: Path, index: int):
    table = pq.read_table(out / f"data/chunk-000/episode_{index:06d}.parquet")
    state = np.asarray(table["observation.state"].to_pylist(), dtype=np.float32)
    action = np.asarray(table["action"].to_pylist(), dtype=np.float32)
    return table, state, action


def video_markers(out: Path, index: int, key: str) -> list[int]:
    path = out / f"videos/chunk-000/{key}/episode_{index:06d}.mp4"
    return [marker(f) for f in media.decode(path)]


def tasks_of(out: Path) -> list[str]:
    return [
        json.loads(line)["task"]
        for line in (out / "meta/tasks.jsonl").read_text().splitlines()
    ]


def test_a_stable_release_is_reversed_frame_for_frame(rpool):
    _, result, out, record = run_export(rpool, ["in_place"], "stable")
    assert result["ok"] and result["episodes"] == 2 and result["reset_episodes"] == 1
    info = json.loads((out / "meta/info.json").read_text())
    assert info["total_episodes"] == 2 and info["total_tasks"] == 2
    assert tasks_of(out) == [TASKS["in_place"], "Reset: " + TASKS["in_place"]]
    fwd_table, fwd_state, fwd_action = read_episode(out, 0)
    table, state, action = read_episode(out, 1)
    n = len(fwd_state)
    assert len(state) == n == N
    # States are the forward states in reverse order ...
    assert np.allclose(state[:, :6], fwd_state[::-1, :6])
    # ... the action is the next state (not the reversed forward action) ...
    assert np.allclose(action[:-1], state[1:]) and np.allclose(action[-1], state[-1])
    assert not np.allclose(action[:, :6], fwd_action[::-1, :6])
    # ... and every camera really runs backwards, frame for frame.
    for key in ("observation.images.hand", "observation.images.view1"):
        assert video_markers(out, 0, key) == list(range(N))
        assert video_markers(out, 1, key) == list(range(N - 1, -1, -1))
    # Bookkeeping: a new task, new indices, no inherited outcome.
    assert set(table["task_index"].to_pylist()) == {1}
    assert set(fwd_table["task_index"].to_pylist()) == {0}
    assert table["index"].to_pylist() == list(range(N, 2 * N))
    assert np.allclose(table["timestamp"].to_numpy(), np.arange(N) / 10)
    episodes = [
        json.loads(x) for x in (out / "meta/episodes.jsonl").read_text().splitlines()
    ]
    assert episodes[1]["tasks"] == ["Reset: " + TASKS["in_place"]]
    assert "levi_outcome" not in episodes[1]
    assert episodes[1]["levi_reset"]["generation"] == "reversed_source"
    assert (
        record["counts"]["reset_episodes"] == 1
        and record["counts"]["forward_episodes"] == 1
    )
    reset = json.loads((out / "meta/levi_reset.json").read_text())
    assert reset["episodes"][0]["temporal_map"] == [
        {
            "source": str(rpool["root"] / "orig/models/pi/t_in_place/demo_0000"),
            "from": N - 1,
            "to": 0,
            "step": -1,
        }
    ]
    assert json.loads((out / "meta/levi_validation.json").read_text())["ok"]


def test_the_reversed_gripper_command_leads_the_fingers(rpool):
    _, _, out, _ = run_export(rpool, ["in_place"], "lead", direction="reset_only")
    _, state, action = read_episode(out, 0)
    grip = state[:, 6]
    # Forward the fingers finish opening at OPEN_DONE: that is the reversed row
    # at which the command must already say "closed" (action = next state).
    row = N - 1 - OPEN_DONE
    assert state[row, 6] == 1.0 and action[row, 6] == 0.0
    # And it opens (the reversed grasp) as the fingers start to, not after.
    grasp_end = [i for i in range(len(grip)) if grip[i] == 0][-1]
    assert action[grasp_end, 6] == 1.0


def test_a_fall_into_reach_is_cut_out_and_the_rest_stays_real(rpool):
    _, result, out, _record = run_export(rpool, ["in_reach"], "reach")
    assert result["reset_episodes"] == 1
    reset = json.loads((out / "meta/levi_reset.json").read_text())
    release = reset["episodes"][0]["analysis"]["releases"][0]
    assert release["class"] == "in_reach"
    hold, rest = release["hold_row"], release["rest_row"]
    assert hold == RELEASE - 1 and OPEN_DONE <= rest <= REST_LAST
    _, state, action = read_episode(out, 1)
    expected = [i for i in range(N) if not hold < i < rest][::-1]
    assert len(state) == len(expected) == N - (rest - hold - 1)
    for key in ("observation.images.hand", "observation.images.view1"):
        assert video_markers(out, 1, key) == expected
    # The frames of the fall are gone, every kept one is a real frame, and the
    # arm did not move across the seam (it was still).
    seam = expected.index(rest)
    assert expected[seam + 1] == hold
    assert np.abs(state[seam, :3] - state[seam + 1, :3]).max() < 0.011
    # At the seam the gripper command says "close": the fingers are open at the
    # rest frame and closed at the hold frame.
    assert state[seam, 6] == 1.0 and action[seam, 6] == 0.0
    assert reset["episodes"][0]["generation"] == "reversed_source_seam"
    assert reset["episodes"][0]["analysis"]["profile"].startswith("reset-profile")


def test_max_release_in_place_refuses_a_fall(rpool):
    _, _, _out, record = run_export(
        rpool, ["in_place", "in_reach"], "strict", max_release="in_place"
    )
    assert record["counts"]["reset_episodes"] == 1
    assert record["counts"]["excluded"] == {"reset_release_in_reach": 1}


def test_an_object_out_of_reach_is_not_reversed_and_a_recording_is_asked_for(rpool):
    _, result, out, record = run_export(rpool, ["escaped"], "gone")
    assert (
        result["episodes"] == 1 and result["reset_episodes"] == 0
    )  # the forward one stays
    assert record["counts"]["excluded"] == {"reset_release_escaped": 1}
    reset = json.loads((out / "meta/levi_reset.json").read_text())
    assert reset["exported"] == 0
    (request,) = json.loads(
        (out / "meta/levi_reset_capture_requests.json").read_text()
    )["requests"]
    assert request["forward_episode"].endswith("t_escaped/demo_0000")
    assert request["reset_task"] == "Reset: " + TASKS["escaped"]
    assert request["anchor_row"] == RELEASE - 1 and request[
        "hold_width_m"
    ] == pytest.approx(0.03, abs=1e-3)
    assert request["cameras"] == ["observation.images.hand", "observation.images.view1"]
    # Nothing reversed was written for it.
    assert not (
        out / "videos/chunk-000/observation.images.hand/episode_000001.mp4"
    ).exists()


def test_reset_only_with_nothing_reversible_is_an_error(rpool):
    with pytest.raises(ValueError, match="No episode can be reversed"):
        run_export(rpool, ["escaped"], "none", direction="reset_only")
    assert not (rpool["out"] / "none").exists()  # nothing half-written is left


def test_partial_reset_starts_at_the_last_safe_hold(rpool):
    _, result, out, _ = run_export(
        rpool, ["escaped"], "partial", on_ineligible="partial"
    )
    assert result["reset_episodes"] == 1
    _, state, _ = read_episode(out, 1)
    # From the hold frame on, backwards: rows RELEASE-1 .. 0.
    assert len(state) == RELEASE
    assert video_markers(out, 1, "observation.images.hand") == list(
        range(RELEASE - 1, -1, -1)
    )
    episodes = [
        json.loads(x) for x in (out / "meta/episodes.jsonl").read_text().splitlines()
    ]
    assert episodes[1]["levi_reset"]["scope"] == "partial"
    assert episodes[1]["levi_reset"]["generation"] == "partial"


def test_an_arm_that_leaves_at_once_is_never_guessed(rpool):
    _, _, out, record = run_export(rpool, ["leaves"], "leaves")
    assert record["counts"]["excluded"] == {"reset_release_unknown": 1}
    reset = json.loads((out / "meta/levi_reset.json").read_text())
    (analysis,) = reset["analysis_of_excluded"].values()
    assert analysis["releases"][0]["reason"] == "arm_left_before_settle"


def test_only_finished_tasks_are_reversed(rpool):
    _, _, _out, record = run_export(rpool, ["failed"], "failed")
    assert record["counts"]["excluded"] == {"reset_forward_failed": 1}
    assert record["counts"]["reset_episodes"] == 0


def test_forward_only_is_the_export_it_always_was(rpool):
    rec = Recipe(name="r", categories=["rollout"], tasks=[TASKS["in_place"]])
    plain = export.ExportOptions(
        format="lerobot_v21", name="plain", output_dir=str(rpool["out"])
    )
    explicit = export.ExportOptions(
        format="lerobot_v21",
        name="expl",
        output_dir=str(rpool["out"]),
        reset={"direction": "forward_only"},
    )
    for options in (plain, explicit):
        jobs.execute(jobs.plan_export(rec, options))
    a, b = (
        json.loads((rpool["out"] / n / "meta/info.json").read_text())
        for n in ("plain", "expl")
    )
    assert a["total_episodes"] == b["total_episodes"] == 1 and a["total_tasks"] == 1
    assert not (rpool["out"] / "expl/meta/levi_reset.json").exists()


# ------------------------------------------------------------------ bridges


def with_record(rpool, **kw):
    from reset_helpers import make_record

    make_record(rpool["root"] / "orig/models/pi/t_record/demo_0000", **kw)
    scanner.scan()
    forward = str(rpool["root"] / "orig/models/pi/t_escaped/demo_0000")
    record = str(rpool["root"] / "orig/models/pi/t_record/demo_0000")
    return {"bridges": [{"source": forward, "record": record}]}


def test_a_recorded_stretch_completes_what_cannot_be_reversed(rpool):
    reset = with_record(rpool)
    _, result, out, record = run_export(rpool, ["escaped"], "bridged", **reset)
    assert result["reset_episodes"] == 1 and record["counts"]["excluded"] == {}
    # The recording is a source, not an exported episode.
    assert result["episodes"] == 2 and record["counts"]["forward_episodes"] == 1
    _, state, action = read_episode(out, 1)
    m = 30  # the recording's last row is at the anchor
    hold = RELEASE - 1
    assert len(state) == m + hold + 1
    expected = [33 + i for i in range(m)] + list(range(hold, -1, -1))
    assert video_markers(out, 1, "observation.images.hand") == expected
    assert video_markers(out, 1, "observation.images.view1") == expected
    # Joined where the arm is: no jump in the state, and the action at the last
    # recorded row is the first reversed forward state.
    assert np.abs(state[m - 1, :3] - state[m, :3]).max() < 0.011
    assert np.allclose(action[m - 1], state[m])
    reset_doc = json.loads((out / "meta/levi_reset.json").read_text())
    entry = reset_doc["episodes"][0]
    assert entry["generation"] == "recorded_bridge" and entry["join"]["ok"]
    assert entry["join"]["row"] == m - 1 and "visual" in entry["join"]
    assert next(r["source"] for r in entry["temporal_map"]).endswith(
        "t_record/demo_0000"
    )
    assert reset_doc["capture_requests"] == []
    assert json.loads((out / "meta/levi_validation.json").read_text())["ok"]


@pytest.mark.parametrize(
    "bad, reason",
    [
        ({"start": (0.5, 0.0, 0.3)}, "reset_bridge_start_mismatch"),
        ({"grasp": False}, "reset_bridge_no_grasp"),
        ({"reach": False}, "reset_bridge_never_reaches_anchor"),
        ({"visual": False}, "reset_bridge_visual_mismatch"),
    ],
)
def test_a_recording_that_does_not_join_is_refused(rpool, bad, reason):
    _, result, _out, record = run_export(
        rpool, ["escaped"], "badbridge", **with_record(rpool, **bad)
    )
    assert result["reset_episodes"] == 0
    assert record["counts"]["excluded"] == {reason: 1}


def test_a_bridge_needs_a_separate_recording_in_the_pool(rpool):
    reset = with_record(rpool)
    forward = reset["bridges"][0]["source"]
    with pytest.raises(ValueError, match="separate recording"):
        run_export(
            rpool, ["escaped"], "b1", bridges=[{"source": forward, "record": forward}]
        )
    with pytest.raises(ValueError, match="not in the pool index"):
        run_export(
            rpool,
            ["escaped"],
            "b2",
            bridges=[{"source": forward, "record": "/nowhere"}],
        )
    with pytest.raises(ValueError, match="not in the selection"):
        run_export(rpool, ["in_place"], "b3", **reset)


def test_a_held_out_recording_cannot_complete_a_reset(rpool, monkeypatch):
    import hashlib

    reset = with_record(rpool)
    demo = rpool["root"] / "orig/models/pi/t_record/demo_0000"
    sha = {
        c: hashlib.sha256((demo / c).read_bytes()).hexdigest()
        for c in ("side_camera.mp4", "wrist_camera.mp4")
    }
    listing = rpool["tmp"] / "frozen.json"
    listing.write_text(
        json.dumps(
            {
                "version": "t",
                "episodes": [
                    {
                        "path": "orig/models/pi/t_record/demo_0000",
                        "frame_count": 30,
                        "sha256": sha,
                        "frozen_id": "F0",
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("LEVI_POOL_HELDOUT", str(listing))
    scanner.scan()
    with pytest.raises(PermissionError, match="held-out"):
        run_export(rpool, ["escaped"], "heldout", **reset)
    assert not (rpool["out"] / "heldout").exists()


def test_an_export_that_is_itself_a_reset_is_not_reversed_again(rpool, monkeypatch):
    _, _, out, _ = run_export(rpool, ["in_place"], "first", direction="reset_only")
    # Offer the export as a pool source (it sits beside the others).
    target = rpool["root"] / "lerobot/first"
    target.parent.mkdir(parents=True)
    import shutil

    shutil.copytree(out, target)
    declare_grippers(rpool["root"])
    scanner.scan()
    rec = Recipe(name="r2", formats=["lerobot"], tasks=["Reset: " + TASKS["in_place"]])
    options = export.ExportOptions(
        format="lerobot_v21",
        name="second",
        output_dir=str(rpool["out"]),
        reset={"direction": "reset_only", "require_forward_success": False},
    )
    with pytest.raises(ValueError, match="reset_already_reset"):
        jobs.execute(jobs.plan_export(rec, options))
    # Without that switch an unlabelled source is not reversed either.
    options = export.ExportOptions(
        format="lerobot_v21",
        name="third",
        output_dir=str(rpool["out"]),
        reset={"direction": "reset_only"},
    )
    with pytest.raises(ValueError, match="reset_forward_unlabeled"):
        jobs.execute(jobs.plan_export(rec, options))


def test_an_interrupted_reset_export_resumes_to_the_same_dataset(rpool, monkeypatch):
    from test_pool_resume import _signature

    from levi.pool import joblog
    from levi.pool.reset import build

    def plan(name):
        rec = Recipe(
            name="r",
            categories=["rollout"],
            tasks=[TASKS["in_place"], TASKS["in_reach"]],
        )
        options = export.ExportOptions(
            format="lerobot_v21",
            name=name,
            output_dir=str(rpool["out"]),
            workers=1,
            reset={"direction": "forward_and_reset"},
        )
        return jobs.plan_export(rec, options)

    clean = plan("clean")
    assert jobs.run_worker(jobs._path(clean["id"])) == 0

    job = plan("cut")
    real = media.encode
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        if calls["n"] >= 2:  # the first reset episode's two cameras are done
            joblog.TERMINATING.set()
            raise SystemExit(143)
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(build.media, "encode", flaky)
    assert jobs.run_worker(jobs._path(job["id"])) == 143
    partial = rpool["out"] / ".cut.partial"
    assert partial.is_dir() and not (rpool["out"] / "cut").exists()
    from levi.pool import journal

    done = [u for u in journal.Journal.read(partial).units if u.startswith("reset|")]
    assert len(done) == 2

    monkeypatch.setattr(build.media, "encode", real)
    joblog.TERMINATING.clear()
    jobs.prepare_resume(job["id"])
    assert jobs.run_worker(jobs._path(job["id"])) == 0
    assert not partial.exists()
    assert _signature(rpool["out"] / "cut") == _signature(rpool["out"] / "clean")
    assert not list((rpool["out"] / "cut").rglob(".reset-scratch"))


# ------------------------------------------------------------------ analysis only, and the model's veto


class FakeClient:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def chat(self, model, digest, messages, **kw):
        self.calls.append((model, len(messages[0]["images"])))
        return {"content": json.dumps(self.answer)}


def _config():
    from types import SimpleNamespace

    return SimpleNamespace(model="m", model_digest="d", context_tokens=4096)


def _rows(rpool, tasks):
    from levi.pool import index

    return [
        r
        for r in index.episodes(limit=100)["episodes"]
        if r["task"] in [TASKS[t] for t in tasks]
    ]


def test_the_analysis_alone_reports_every_release(rpool):
    from levi.conversion.options import Options
    from levi.pool.reset import preview

    rows = _rows(rpool, ["in_place", "in_reach", "escaped", "leaves", "failed"])
    out = preview.analyze_rows(
        rows,
        ResetOptions(direction="reset_only"),
        Options(filter_static=False, timing="retime"),
    )
    by_task = {v["task"]: v for v in out["episodes"]}
    assert (
        by_task[TASKS["in_place"]]["eligible"]
        and by_task[TASKS["in_reach"]]["eligible"]
    )
    assert by_task[TASKS["escaped"]]["reason"] == "reset_release_escaped"
    assert by_task[TASKS["leaves"]]["reason"] == "reset_release_unknown"
    assert by_task[TASKS["failed"]]["reason"] == "reset_forward_failed"
    assert out["summary"]["reversible"] == 2
    assert out["summary"]["release_classes"] == {
        "in_place": 1,
        "in_reach": 1,
        "escaped": 1,
        "unknown": 1,
    }
    first = by_task[TASKS["in_reach"]]["releases"][0]
    assert first["metrics"]["best"] > 0.6 and first["seam"]["position_jump"] < 0.011


def test_a_vision_model_can_veto_but_never_rescue(rpool):
    from levi.conversion.options import Options
    from levi.pool.reset import preview, review

    conv, opts = (
        Options(filter_static=False, timing="retime"),
        ResetOptions(direction="reset_only"),
    )
    reach = _rows(rpool, ["in_reach"])[0]
    gone = _rows(rpool, ["escaped"])[0]
    no = review.VlmReviewer(
        _config(), FakeClient({"object_in_reach": False, "note": "rolled away"})
    )
    yes = review.VlmReviewer(
        _config(), FakeClient({"object_in_reach": True, "note": "between the fingers"})
    )
    vetoed = preview.analyze_row(reach, opts, conv, reviewer=no)
    assert not vetoed["eligible"] and vetoed["releases"][0]["reason"] == "vlm_veto"
    assert vetoed["releases"][0]["metrics"]["vlm"]["note"] == "rolled away"
    assert no.client.calls == [("m", 2)]  # one call, both frames
    kept = preview.analyze_row(reach, opts, conv, reviewer=yes)
    assert kept["eligible"]
    rescued = preview.analyze_row(gone, opts, conv, reviewer=yes)
    assert not rescued["eligible"] and yes.client.calls == [("m", 2)]  # not even asked


# ------------------------------------------------------------------ API and CLI


def test_the_api_analyzes_and_exports_a_reset(rpool, client):
    recipe_body = {
        "name": "api",
        "categories": ["rollout"],
        "tasks": [TASKS["in_place"], TASKS["escaped"], TASKS["in_reach"]],
    }
    out = client.post(
        "/api/levi/pool/reset/analyze", json={"recipe": recipe_body, "limit": 5}
    ).json()
    assert out["selected"] == 3 and out["analyzed"] == 3
    assert out["summary"]["reversible"] == 2
    assert out["summary"]["reasons"] == {"reset_release_escaped": 1}
    assert out["profile"].startswith("reset-profile")
    bad = client.post(
        "/api/levi/pool/reset/analyze",
        json={"recipe": recipe_body, "reset": {"direction": "reset_only", "x": 1}},
    )
    assert bad.status_code == 422 or bad.status_code == 400
    dry = client.post(
        "/api/levi/pool/export",
        json={
            "recipe": recipe_body,
            "dry_run": True,
            "options": {
                "format": "lerobot_v21",
                "name": "viaapi",
                "output_dir": str(rpool["out"]),
                "reset": {"direction": "forward_and_reset"},
            },
        },
    ).json()
    assert dry["options"]["reset"]["direction"] == "forward_and_reset"
    assert dry["options"]["filter_static"] is False and "bridge_records" not in dry
    refused = client.post(
        "/api/levi/pool/export",
        json={
            "recipe": recipe_body,
            "dry_run": True,
            "options": {
                "format": "recap_value",
                "name": "nope",
                "reset": {"direction": "reset_only"},
            },
        },
    )
    assert refused.status_code in (400, 422)


def test_the_cli_exports_and_analyzes(rpool, capsys):
    from levi.pool import cli, recipe

    recipe.save(Recipe(name="cli", categories=["rollout"], tasks=[TASKS["in_place"]]))
    assert cli.main(["reset-analyze", "cli", "--limit", "3"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["summary"]["reversible"] == 1
    code = cli.main(
        [
            "export", "cli", "--format", "lerobot_v21", "--name", "viacli",
            "--output-dir", str(rpool["out"]), "--reset", "forward_and_reset",
            "--reset-template", "Undo: {task}",
        ]
    )  # fmt: skip
    assert code == 0
    assert tasks_of(rpool["out"] / "viacli") == [
        TASKS["in_place"],
        "Undo: " + TASKS["in_place"],
    ]
