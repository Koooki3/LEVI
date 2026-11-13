"""Prepare source-bound reset observations and describe policy action proposals.

This reads saved files only. Replaying recorded images after changing an action
is not a closed-loop experiment. Neither gripper command nor aperture proves
holding, grasp readiness, contact or successful reset.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from itertools import pairwise
from pathlib import Path

import numpy as np

from .offline_worker import atomic_json

TASKS = (
    "Reset_pick_the_eggplant_in_the_blue_plate",
    "Reset_pick_the_watermelon_into_the_blue_plate",
)
SOURCE_FILES = (
    "metadata.json",
    "frames.csv",
    "end_effector_pose.csv",
    "gripper_state.csv",
    "robot_joint_states.csv",
    "events.csv",
    "side_camera.mp4",
    "wrist_camera.mp4",
)
LIMITATIONS = [
    "recorded_observations_are_not_a_counterfactual_closed_loop",
    "original_policy_chunks_and_sampling_noise_not_recorded",
    "grasp_geometry_ground_truth_unavailable",
    "holding_unknown_aperture_is_not_object_follow_evidence",
    "wall_clock_read_time_is_not_camera_exposure_or_hardware_timestamp",
    "new_inference_does_not_exactly_reproduce_historical_model_actions",
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _finite(value: str | float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"non-finite {name}")
    return number


def _rows(path: Path) -> list[dict]:
    with path.open(newline="") as stream:
        result = list(csv.DictReader(stream))
    if not result:
        raise ValueError(f"empty CSV: {path.name}")
    if [int(row["frame_index"]) for row in result] != list(range(1, len(result) + 1)):
        raise ValueError(f"non-contiguous frame_index: {path.name}")
    timestamps = [_finite(row["timestamp_sec"], "timestamp") for row in result]
    if any(b <= a for a, b in pairwise(timestamps)):
        raise ValueError(f"non-increasing clock: {path.name}")
    return result


def read_episode(directory: Path) -> dict:
    """Validate identity and row-clock alignment before selecting observations."""
    metadata = json.loads((directory / "metadata.json").read_text())
    policy = metadata["policy"]
    server = policy["server_metadata"]
    if (
        policy.get("config") != "reset_r1_pi05"
        or Path(policy.get("checkpoint_dir", "")).name != "reset_r1_step20000"
        or server.get("artifact") != "reset_r1_step20000"
        or server.get("checkpoint_step") != 20000
        or server.get("gripper_convention")
        != "1.0=open, 0.0=closed, commanded not measured"
        or server.get("rx_output_branch") != "continuous"
    ):
        raise ValueError("reset checkpoint/input contract mismatch")
    stroke = _finite(metadata["gripper_control"]["stroke_m"], "stroke_m")
    if stroke <= 0:
        raise ValueError("stroke_m must be positive")
    rows = {
        name: _rows(directory / name)
        for name in (
            "frames.csv",
            "end_effector_pose.csv",
            "gripper_state.csv",
            "robot_joint_states.csv",
        )
    }
    frames = rows["frames.csv"]
    count = len(frames)
    if count != metadata["frame_count"]:
        raise ValueError("metadata frame count mismatch")
    clock_differences = {}
    for name, table in rows.items():
        if len(table) != count:
            raise ValueError(f"CSV row count mismatch: {name}")
        difference = max(
            abs(float(a["timestamp_sec"]) - float(b["timestamp_sec"]))
            for a, b in zip(frames, table)
        )
        clock_differences[name] = difference
        if difference > 1e-6:
            raise ValueError(f"CSV clock mismatch: {name}: {difference}")
    for row in frames:
        if (
            row.get("side_video") != "side_camera.mp4"
            or row.get("wrist_video") != "wrist_camera.mp4"
        ):
            raise ValueError("unexpected frame-to-video mapping")
    gripper = rows["gripper_state.csv"]
    for row in gripper:
        commanded = _finite(row["commanded_width_m"], "commanded_width_m")
        measured = _finite(row["gripper_width"], "gripper_width")
        command = row["last_gripper_command"]
        if command not in {"open", "close"}:
            raise ValueError("unknown gripper command")
        expected = stroke if command == "open" else 0.0
        if abs(commanded - expected) > 1e-6 or not 0 <= measured <= stroke + 1e-6:
            raise ValueError("gripper command/measurement contract mismatch")
    first_close = next(
        (
            index
            for index, row in enumerate(gripper)
            if row["last_gripper_command"] == "close"
        ),
        None,
    )
    if first_close is None or first_close == 0 or first_close + 8 >= count:
        raise ValueError("missing pre-close/first-close/post-close observations")
    prompt = metadata["task_description"]
    normalise = lambda text: text.strip().replace("_", " ").replace("\n", " ")
    trained = {normalise(value) for value in server["trained_prompts"]}
    selected = [first_close - 1, first_close, first_close + 8]
    summary = {
        "source": str(directory),
        "frame_count": count,
        "prompt": prompt,
        "prompt_in_training": normalise(prompt) in trained,
        "historical_unknown_prompt_allowed": server.get("prompt_policy") == "any",
        "first_close_frame_index": first_close + 1,
        "first_close_elapsed_s": float(gripper[first_close]["timestamp_sec"])
        - float(gripper[0]["timestamp_sec"]),
        "close_command_rows": sum(
            row["last_gripper_command"] == "close" for row in gripper
        ),
        "reopen_after_first_close": any(
            row["last_gripper_command"] == "open" for row in gripper[first_close:]
        ),
        "measured_aperture_min_m": min(float(row["gripper_width"]) for row in gripper),
        "measured_aperture_max_m": max(float(row["gripper_width"]) for row in gripper),
        "maximum_csv_clock_difference_s": clock_differences,
        "maximum_recording_gap_s": max(
            float(b["timestamp_sec"]) - float(a["timestamp_sec"])
            for a, b in pairwise(frames)
        ),
        "human_episode_outcome": metadata.get("eval", {}).get("operator_outcome"),
        "run_id": metadata.get("eval", {}).get("run_id"),
        "holding_status": "unknown",
        "grasp_geometry_ground_truth": "unavailable",
    }
    return {
        "metadata": metadata,
        "rows": rows,
        "selected": selected,
        "summary": summary,
        "stroke_m": stroke,
    }


def _state7(episode: dict, row_index: int) -> np.ndarray:
    pose = episode["rows"]["end_effector_pose.csv"][row_index]
    grip = episode["rows"]["gripper_state.csv"][row_index]
    xyz = [_finite(pose[key], key) for key in ("px", "py", "pz")]
    quat = np.array(
        [_finite(pose[key], key) for key in ("qx", "qy", "qz", "qw")], dtype=np.float32
    ).astype(np.float64)
    norm = float(np.linalg.norm(quat))
    if abs(norm - 1.0) > 1e-3:
        raise ValueError("recorded quaternion is not normalized")
    # Match the client's float32 quaternion input to scipy's xyz conversion.
    # Internal calculations use double precision, as scipy does. The checkpoint's
    # loader alone performs rx lifting/rebasing; do not apply either here.
    x, y, z, w = quat / norm
    sin_pitch = float(2 * (w * y - z * x))
    if abs(sin_pitch) >= 1 - 1e-7:
        raise ValueError("gimbal-lock observation requires explicit branch review")
    euler = (
        math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)),
        math.asin(sin_pitch),
        math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)),
    )
    command = float(grip["commanded_width_m"]) / episode["stroke_m"]
    return np.asarray([*xyz, *euler, command], dtype=np.float32)


def _images(path: Path, selected: list[int], expected_count: int) -> tuple[dict, dict]:
    import cv2

    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise ValueError(f"cannot open saved video: {path.name}")
        if round(capture.get(cv2.CAP_PROP_FRAME_COUNT)) != expected_count:
            raise ValueError(f"video frame count mismatch: {path.name}")
        fps = capture.get(cv2.CAP_PROP_FPS)
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError("invalid recorded video FPS")
        images, timing = {}, {}
        for index in selected:
            if not capture.set(cv2.CAP_PROP_POS_FRAMES, index):
                raise ValueError("saved video seek failed")
            ok, bgr = capture.read()
            if (
                not ok
                or bgr is None
                or round(capture.get(cv2.CAP_PROP_POS_FRAMES)) != index + 1
            ):
                raise ValueError("saved video frame read/mapping failed")
            images[index] = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            timing[index] = {
                "video_frame_index_zero_based": index,
                "decoder_pts_s": capture.get(cv2.CAP_PROP_POS_MSEC) / 1000,
                "container_fps": fps,
            }
        return images, timing
    finally:
        capture.release()


def prepare_samples(output_root: Path, *, source_root: Path) -> list[dict]:
    """Read ten reset recordings and write three bound NPZ observations each.

    output_root must be new or contain no samples.json/samples directory. Paths
    are explicit, so this module never assumes a developer's home directory.
    """
    output_root, source_root = Path(output_root).resolve(), Path(source_root).resolve()
    if output_root.is_relative_to(source_root) or source_root.is_relative_to(
        output_root
    ):
        raise ValueError("source and output directories must be disjoint")
    if (output_root / "samples.json").exists() or (output_root / "samples").exists():
        raise FileExistsError(
            "sample output already exists; use a new output directory"
        )
    episodes = []
    for task in TASKS:
        for index in range(5):
            directory = source_root / task / f"demo_{index:04d}"
            if not (directory / ".complete").is_file():
                raise ValueError("source recording is not complete")
            episode = read_episode(directory)
            episode["source_hashes"] = {
                name: sha256(directory / name) for name in SOURCE_FILES
            }
            episodes.append((directory, episode))
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "samples").mkdir()
    samples = []
    for directory, episode in episodes:
        selected = episode["selected"]
        count = episode["summary"]["frame_count"]
        side, side_time = _images(directory / "side_camera.mp4", selected, count)
        wrist, wrist_time = _images(directory / "wrist_camera.mp4", selected, count)
        for phase, index in zip(
            ("pre_close", "first_close", "post_close_8"), selected, strict=True
        ):
            sample_id = f"{directory.parent.name}__{directory.name}__f{index + 1:04d}"
            path = output_root / "samples" / f"{sample_id}.npz"
            state = _state7(episode, index)
            np.savez_compressed(
                path, image=side[index], wrist_image=wrist[index], state7=state
            )
            row = episode["rows"]["gripper_state.csv"][index]
            samples.append(
                {
                    "sample_id": sample_id,
                    "npz_path": str(path),
                    "npz_sha256": sha256(path),
                    "prompt": episode["summary"]["prompt"],
                    "phase": phase,
                    "source": str(directory),
                    "source_hashes": episode["source_hashes"],
                    "frame_index": index + 1,
                    "csv_row_index_zero_based": index,
                    "recorded_wall_time_s": float(row["timestamp_sec"]),
                    "recorded_elapsed_s": float(row["timestamp_sec"])
                    - float(episode["rows"]["gripper_state.csv"][0]["timestamp_sec"]),
                    "image_mapping": {
                        "side": side_time[index],
                        "wrist": wrist_time[index],
                    },
                    "state7": state.tolist(),
                    "commanded_gripper_open_fraction": float(state[6]),
                    "measured_gripper_width_m": float(row["gripper_width"]),
                    "measured_gripper_open_fraction": float(row["gripper_width"])
                    / episode["stroke_m"],
                    "prompt_in_training": episode["summary"]["prompt_in_training"],
                    "allow_unknown_prompt_required": not episode["summary"][
                        "prompt_in_training"
                    ],
                    "holding_status": "unknown",
                    "grasp_readiness": "unknown",
                    "grasp_geometry_ground_truth": "unavailable",
                    "historical_action_ground_truth": "unavailable",
                    "physical_execution": False,
                }
            )
    for directory, episode in episodes:
        if any(
            sha256(directory / name) != digest
            for name, digest in episode["source_hashes"].items()
        ):
            raise ValueError("source changed during sample extraction")
    manifest = {
        "schema": "levi.reset_policy.samples.v1",
        "samples": samples,
        "source_summary": {
            "episodes": [episode["summary"] for _, episode in episodes],
            "episode_count": len(episodes),
            "sample_count": len(samples),
            "protected_inputs_unchanged": True,
        },
        "state_contract": {
            "pose": "recorded_xyz_plus_scipy_quaternion_xyzw_to_xyz_euler",
            "rx_branch_transform": "checkpoint_loader_only",
            "gripper": "commanded_width_m_divided_by_recorded_stroke_m",
            "historical_model_input_exact": False,
        },
        "physical_execution": False,
        "closed_loop": False,
        "limitations": LIMITATIONS,
    }
    atomic_json(output_root / "samples.json", manifest)
    return samples


def summarize_action_chunk(actions, state7) -> dict:
    """Numerical descriptions only; no reachability or success classification."""
    values, state = (
        np.asarray(actions, dtype=np.float64),
        np.asarray(state7, dtype=np.float64),
    )
    if (
        values.ndim != 2
        or values.shape[1] != 7
        or not len(values)
        or not np.isfinite(values).all()
    ):
        raise ValueError("expected a nonempty finite (horizon, 7) action chunk")
    if state.shape != (7,) or not np.isfinite(state).all():
        raise ValueError("expected a finite state7")
    grip = values[:, 6]
    close = grip < 0.5
    indices = np.flatnonzero(close)
    rotation_delta = (values[0, 3:6] - state[3:6] + np.pi) % (2 * np.pi) - np.pi
    return {
        "action_count": len(values),
        "first_action_gripper_raw": float(grip[0]),
        "gripper_raw_min": float(grip.min()),
        "gripper_raw_max": float(grip.max()),
        "close_proposal_rows": int(close.sum()),
        "all_rows_propose_close": bool(close.all()),
        "first_close_proposal_row": int(indices[0]) if len(indices) else None,
        "gripper_outside_unit_interval_rows": int(((grip < 0) | (grip > 1)).sum()),
        "first_position_target_minus_observation_m": (
            values[0, :3] - state[:3]
        ).tolist(),
        "first_rotation_modulo_delta_rad": rotation_delta.tolist(),
        "holding_status": "unknown",
        "grasp_readiness": "unknown",
        "premature_close_ground_truth": "unavailable",
        "physical_execution": False,
    }


def summarize_policy_outputs(samples: list[dict], predictions: list[dict]) -> dict:
    """Join sample IDs to raw outputs without inventing executed effects."""
    by_id = {sample["sample_id"]: sample for sample in samples}
    if len(by_id) != len(samples):
        raise ValueError("duplicate sample identity")
    seen, results = set(), []
    for prediction in predictions:
        sample_id = prediction["sample_id"]
        identity = (sample_id, prediction.get("seed"))
        if sample_id not in by_id or identity in seen:
            raise ValueError("unknown or repeated prediction identity")
        seen.add(identity)
        sample = by_id[sample_id]
        results.append(
            {
                "sample_id": sample_id,
                "seed": prediction.get("seed"),
                "phase": sample["phase"],
                "prompt_in_training": sample["prompt_in_training"],
                **summarize_action_chunk(prediction["actions"], sample["state7"]),
            }
        )
    return {
        "schema": "levi.reset_policy.action_summary.v1",
        "results": results,
        "prediction_count": len(results),
        "physical_execution": False,
        "closed_loop": False,
        "success_rate": None,
        "limitations": LIMITATIONS,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    samples = prepare_samples(args.output_root, source_root=args.source_root)
    print(
        f"prepared {len(samples)} source-bound observations: {args.output_root / 'samples.json'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
