"""Collector-compatible video/CSV sink, committed only after acknowledged motion."""

import csv
import os
import time
from pathlib import Path

from .recorder import RolloutRecorder


class Media:
    def __init__(self, cv2, rotation, online_module, judge, hz):
        self.cv2, self.rotation = cv2, rotation
        self.online, self.judge, self.hz = online_module, judge, hz
        self.active = {}
        self.buffers = {}

    def open(self, folder):
        folder = Path(folder)
        headers = {
            "frames.csv": [
                "timestamp_sec",
                "frame_index",
                "success_flag",
                "wrist_video",
                "side_video",
            ],
            "end_effector_pose.csv": [
                "timestamp_sec",
                "frame_index",
                "success_flag",
                "source_stamp_sec",
                "px",
                "py",
                "pz",
                "qx",
                "qy",
                "qz",
                "qw",
            ],
            "gripper_state.csv": [
                "timestamp_sec",
                "frame_index",
                "success_flag",
                "source_stamp_sec",
                "finger_left",
                "finger_right",
                "gripper_width",
                "last_gripper_command",
                "commanded_width_m",
            ],
            "robot_joint_states.csv": [
                "timestamp_sec",
                "frame_index",
                "success_flag",
                "source_stamp_sec",
                *[f"joint_{i}_pos" for i in range(1, 8)],
                *[f"joint_{i}_vel" for i in range(1, 8)],
                *[f"joint_{i}_eff" for i in range(1, 8)],
            ],
        }
        files, writers = {}, {}
        try:
            for name, header in headers.items():
                files[name] = (folder / name).open("x", newline="")
                writers[name] = csv.writer(files[name])
                writers[name].writerow(header)
        except BaseException:
            for handle in files.values():
                handle.close()
            raise
        self.active[folder] = {
            "files": files,
            "csv": writers,
            "video": {},
            "pending": None,
            "count": 0,
            "episode": None,
        }

    def frame(self, folder, step, obs):
        self.active[Path(folder)]["pending"] = obs

    def commit(self, folder, episode_id):
        row = self.active[Path(folder)]
        obs, row["pending"] = row["pending"], None
        if obs is None:
            raise ValueError("media observation missing")
        images = obs["images_bgr"]
        for view in ("side", "wrist"):
            pixels = images[view]
            if view not in row["video"]:
                h, w = pixels.shape[:2]
                writer = self.cv2.VideoWriter(
                    str(Path(folder) / (view + "_camera.mp4")),
                    self.cv2.VideoWriter_fourcc(*"mp4v"),
                    self.hz,
                    (w, h),
                )
                if not writer.isOpened():
                    writer.release()
                    raise OSError("video writer unavailable")
                row["video"][view] = writer
            row["video"][view].write(pixels)
        n = row["count"] = row["count"] + 1
        stamp = time.time()
        prefix = [stamp, n, 0, stamp]
        pose = obs["pose"]
        quat = self.rotation.from_euler("xyz", pose[3:6]).as_quat().tolist()
        row["csv"]["frames.csv"].writerow(
            [stamp, n, 0, "wrist_camera.mp4", "side_camera.mp4"]
        )
        row["csv"]["end_effector_pose.csv"].writerow([*prefix, *pose[:3], *quat])
        width = float(obs["robot_state"]["gripper_position"]) * 0.085
        command = "open" if pose[6] >= 0.5 else "close"
        row["csv"]["gripper_state.csv"].writerow(
            [*prefix, width / 2, width / 2, width, command, pose[6] * 0.085]
        )
        q = [float(x) for x in obs["robot_state"]["joint_positions"]]
        row["csv"]["robot_joint_states.csv"].writerow(
            [*prefix, *q, *([float("nan")] * 14)]
        )
        buffer = self.buffers.get(episode_id)
        if buffer is None:
            buffer = self.judge.new_buffer(self.hz)
            self.buffers[episode_id] = buffer
        buffer.add(n, stamp, obs["raw"]["image"])
        row["episode"] = episode_id

    def _close(self, folder):
        folder = Path(folder)
        row = self.active.get(folder)
        first_error = None
        if row:
            try:
                for writer in row["video"].values():
                    try:
                        writer.release()
                    except BaseException as exc:  # noqa: BLE001 - attempt every owned resource
                        first_error = first_error or exc
                for handle in row["files"].values():
                    try:
                        handle.flush()
                        os.fsync(handle.fileno())
                    except BaseException as exc:  # noqa: BLE001 - attempt every owned resource
                        first_error = first_error or exc
                    finally:
                        try:
                            handle.close()
                        except BaseException as exc:  # noqa: BLE001 - attempt every owned resource
                            first_error = first_error or exc
            finally:
                self.active.pop(folder, None)
                if row["episode"]:
                    self.buffers.pop(row["episode"], None)
        if first_error is not None:
            raise first_error
        return row

    def finish(self, folder, frames):
        row = self._close(folder)
        if row and row["episode"]:
            self.buffers.pop(row["episode"], None)
        return {
            "video_frames_match_csv": row is not None and row["count"] == frames,
            "stalled": [],
            "videos": [v + "_camera.mp4" for v in ("side", "wrist")],
        }

    def abort(self, folder):
        row = self._close(folder)
        if row and row["episode"]:
            self.buffers.pop(row["episode"], None)

    def close(self):
        first_error = None
        for folder in list(self.active):
            try:
                self._close(folder)
            except BaseException as exc:  # noqa: BLE001 - attempt every owned resource
                first_error = first_error or exc
        self.buffers.clear()
        if first_error is not None:
            raise first_error


class Recorder(RolloutRecorder):
    def commit(self, rollout, result):
        self.media.commit(rollout.path, rollout.episode_id)
        super().commit(rollout, result)

    def close(self):
        first_error = None
        for operation in (super().close, self.media.close):
            try:
                operation()
            except BaseException as exc:  # noqa: BLE001 - attempt every owned resource
                first_error = first_error or exc
        if first_error is not None:
            raise first_error
