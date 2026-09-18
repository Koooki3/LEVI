"""Robot captures whose cameras are folders of numbered images instead of
videos. Same CSV contract as ``robot_capture``; frames are declared at
``options.source_fps`` because image files carry no timing."""

from pathlib import Path

from .robot_capture import RobotCapture, demo_dirs


class ImageSequence(RobotCapture):
    id = "image_sequence"
    label = "Robot capture (pose/gripper CSV + image folders)"
    description = (
        "task/demo_NNNN folders with the capture CSVs and one folder of "
        "numbered PNG/JPEG frames per camera (rate taken from source_fps)."
    )
    evidence = "fixture"

    def detect(self, root: Path) -> float:
        for demo in demo_dirs(root)[:20]:
            if (demo / "end_effector_pose.csv").is_file() and any(
                any(p.glob("*.png")) or any(p.glob("*.jpg"))
                for p in demo.iterdir()
                if p.is_dir()
            ):
                return 0.9
        return 0.0
