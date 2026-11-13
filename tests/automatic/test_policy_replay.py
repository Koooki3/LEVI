"""Saved-source contracts; synthetic files are confined to pytest's workspace."""

import csv
import json

import numpy as np
import pytest

from levi.automatic import policy_replay as replay


def write_csv(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def episode(directory, *, count=12):
    directory.mkdir(parents=True)
    (directory / ".complete").touch()
    metadata = {
        "policy": {
            "config": "reset_r1_pi05",
            "checkpoint_dir": "/fixture/reset_r1_step20000",
            "server_metadata": {
                "artifact": "reset_r1_step20000",
                "checkpoint_step": 20000,
                "gripper_convention": "1.0=open, 0.0=closed, commanded not measured",
                "rx_output_branch": "continuous",
                "trained_prompts": ["Reset: known"],
                "prompt_policy": "any",
            },
        },
        "gripper_control": {"stroke_m": 0.08},
        "frame_count": count,
        "task_description": "Reset: unseen",
        "eval": {"operator_outcome": "failure", "run_id": "fixture"},
    }
    (directory / "metadata.json").write_text(json.dumps(metadata))
    frames, poses, grips, joints = [], [], [], []
    for index in range(count):
        common = {"frame_index": index + 1, "timestamp_sec": 100 + index * 0.1}
        frames.append(
            {
                **common,
                "side_video": "side_camera.mp4",
                "wrist_video": "wrist_camera.mp4",
            }
        )
        # Rotation of pi about x; pose fields deliberately use xyzw order.
        poses.append(
            {
                **common,
                "px": 0.3,
                "py": 0.1,
                "pz": 0.2,
                "qx": 1.0,
                "qy": 0.0,
                "qz": 0.0,
                "qw": 0.0,
            }
        )
        grips.append(
            {
                **common,
                "gripper_width": 0.04,
                "last_gripper_command": "open" if index < 2 else "close",
                "commanded_width_m": 0.08 if index < 2 else 0.0,
            }
        )
        joints.append({**common, "joint_1_pos": 0.0})
    for name, rows in (
        ("frames.csv", frames),
        ("end_effector_pose.csv", poses),
        ("gripper_state.csv", grips),
        ("robot_joint_states.csv", joints),
    ):
        write_csv(directory / name, rows)
    for name in ("side_camera.mp4", "wrist_camera.mp4", "events.csv"):
        (directory / name).write_bytes(b"fixture-only")
    return directory


def test_selection_uses_command_transition_not_measured_aperture(tmp_path):
    data = replay.read_episode(episode(tmp_path / "episode"))
    assert data["selected"] == [1, 2, 10]
    assert data["summary"]["first_close_frame_index"] == 3
    assert data["summary"]["close_command_rows"] == 10
    assert data["summary"]["reopen_after_first_close"] is False
    assert data["summary"]["prompt_in_training"] is False
    assert data["summary"]["holding_status"] == "unknown"
    before, after = replay._state7(data, 1), replay._state7(data, 2)
    np.testing.assert_allclose(before, [0.3, 0.1, 0.2, np.pi, 0, 0, 1], atol=1e-6)
    assert after[6] == 0  # Aperture remains 0.04 m / 50%; it is not the model state.


@pytest.mark.parametrize(
    "file", ["end_effector_pose.csv", "gripper_state.csv", "robot_joint_states.csv"]
)
def test_cross_csv_clock_mismatch_is_refused(tmp_path, file):
    path = episode(tmp_path / "episode")
    with (path / file).open() as stream:
        rows = list(csv.DictReader(stream))
    rows[3]["timestamp_sec"] = 100.35
    write_csv(path / file, rows)
    with pytest.raises(ValueError, match="CSV clock mismatch"):
        replay.read_episode(path)


@pytest.mark.parametrize(
    "mutation",
    ["duplicate_index", "duplicate_time", "count", "nonfinite", "unknown_command"],
)
def test_invalid_source_rows_fail_closed(tmp_path, mutation):
    path = episode(tmp_path / "episode")
    with (path / "gripper_state.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    if mutation == "duplicate_index":
        rows[3]["frame_index"] = rows[2]["frame_index"]
    elif mutation == "duplicate_time":
        rows[3]["timestamp_sec"] = rows[2]["timestamp_sec"]
    elif mutation == "count":
        rows.pop()
    elif mutation == "nonfinite":
        rows[3]["gripper_width"] = "nan"
    else:
        rows[3]["last_gripper_command"] = "unknown"
    write_csv(path / "gripper_state.csv", rows)
    with pytest.raises(ValueError):
        replay.read_episode(path)


def test_unsupported_checkpoint_and_invalid_rotation_refused(tmp_path):
    path = episode(tmp_path / "episode")
    data = replay.read_episode(path)
    data["rows"]["end_effector_pose.csv"][0]["qx"] = "0"
    with pytest.raises(ValueError, match="quaternion"):
        replay._state7(data, 0)
    meta = json.loads((path / "metadata.json").read_text())
    meta["policy"]["config"] = "different_model"
    (path / "metadata.json").write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="contract mismatch"):
        replay.read_episode(path)


def test_gimbal_lock_is_refused_instead_of_guessing_a_branch(tmp_path):
    data = replay.read_episode(episode(tmp_path / "episode"))
    row = data["rows"]["end_effector_pose.csv"][0]
    row.update(qx=0, qy=2**-0.5, qz=0, qw=2**-0.5)
    with pytest.raises(ValueError, match="gimbal-lock"):
        replay._state7(data, 0)


def test_preparation_preserves_sources_and_separates_inputs(tmp_path, monkeypatch):
    source = tmp_path / "source"
    for task in replay.TASKS:
        for index in range(5):
            episode(source / task / f"demo_{index:04d}")
    before = {
        str(path): replay.sha256(path) for path in source.rglob("*") if path.is_file()
    }

    def images(path, selected, expected_count):
        assert expected_count == 12
        marker = 12 if path.name == "side_camera.mp4" else 24
        return (
            {
                index: np.full((3, 4, 3), marker + index, dtype=np.uint8)
                for index in selected
            },
            {
                index: {
                    "video_frame_index_zero_based": index,
                    "decoder_pts_s": index / 10,
                    "container_fps": 10.0,
                }
                for index in selected
            },
        )

    monkeypatch.setattr(replay, "_images", images)
    output = tmp_path / "output"
    samples = replay.prepare_samples(output, source_root=source)
    assert len(samples) == 30
    assert all(row["allow_unknown_prompt_required"] for row in samples)
    assert all(row["holding_status"] == "unknown" for row in samples)
    with np.load(samples[1]["npz_path"], allow_pickle=False) as npz:
        assert set(npz.files) == {"image", "wrist_image", "state7"}
        assert npz["image"][0, 0, 0] == 14
        assert npz["wrist_image"][0, 0, 0] == 26
        assert npz["state7"][6] == 0
    manifest = json.loads((output / "samples.json").read_text())
    assert manifest["source_summary"]["protected_inputs_unchanged"] is True
    assert manifest["closed_loop"] is False
    assert {
        str(path): replay.sha256(path) for path in source.rglob("*") if path.is_file()
    } == before
    with pytest.raises(FileExistsError):
        replay.prepare_samples(output, source_root=source)
    with pytest.raises(ValueError, match="disjoint"):
        replay.prepare_samples(source / "derived", source_root=source)


def test_actual_saved_video_mapping_is_rgb_and_zero_based(tmp_path):
    import cv2

    path = tmp_path / "saved.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (32, 32))
    assert writer.isOpened(), "local OpenCV must be able to write the test fixture"
    for index in range(12):
        frame = np.zeros((32, 32, 3), np.uint8)
        frame[:] = (index * 10, 20, 100)
        writer.write(frame)
    writer.release()
    images, timing = replay._images(path, [1, 2, 10], 12)
    assert images[10].shape == (32, 32, 3)
    # mp4v is lossy; decoded RGB red is ~100 and blue tracks the frame index.
    np.testing.assert_allclose(images[2][16, 16], [100, 20, 20], atol=8)
    assert timing[2]["video_frame_index_zero_based"] == 2
    with pytest.raises(ValueError, match="frame count mismatch"):
        replay._images(path, [1], 11)


def test_action_summary_is_proposal_description_not_success():
    actions = np.tile([0.3, 0.1, 0.2, np.pi, 0, 0, 0.2], (10, 1))
    summary = replay.summarize_action_chunk(actions, [0.3, 0.1, 0.2, -np.pi, 0, 0, 1])
    assert summary["close_proposal_rows"] == 10
    assert summary["all_rows_propose_close"] is True
    assert summary["holding_status"] == "unknown"
    assert summary["premature_close_ground_truth"] == "unavailable"
    np.testing.assert_allclose(summary["first_rotation_modulo_delta_rad"], [0, 0, 0])
    actions[:, 6] = 0.5
    assert (
        replay.summarize_action_chunk(actions, np.zeros(7))["close_proposal_rows"] == 0
    )
    actions[0, 6] = -0.2
    assert (
        replay.summarize_action_chunk(actions, np.zeros(7))[
            "gripper_outside_unit_interval_rows"
        ]
        == 1
    )


def test_action_summary_refuses_bad_shape_and_nonfinite():
    for actions in [
        np.zeros((7,)),
        np.zeros((2, 8)),
        np.zeros((0, 7)),
        np.full((2, 7), np.nan),
    ]:
        with pytest.raises(ValueError):
            replay.summarize_action_chunk(actions, np.zeros(7))


def test_prediction_identity_is_bound_to_samples():
    samples = [
        {
            "sample_id": "one",
            "phase": "first_close",
            "prompt_in_training": False,
            "state7": [0] * 7,
        }
    ]
    predictions = [{"sample_id": "one", "seed": 3, "actions": np.zeros((10, 7))}]
    result = replay.summarize_policy_outputs(samples, predictions)
    assert result["success_rate"] is None and result["physical_execution"] is False
    with pytest.raises(ValueError, match="repeated"):
        replay.summarize_policy_outputs(samples, predictions * 2)
    with pytest.raises(ValueError, match="unknown"):
        replay.summarize_policy_outputs(
            samples, [{**predictions[0], "sample_id": "other"}]
        )
