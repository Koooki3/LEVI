"""Measured joint angles and gripper width in a conversion (joint_states.py).

A tiny synthetic capture (12 frames, 64x48, one camera): no real dataset is
read or written. The captures carry ``robot_joint_states.csv`` the way
``levi/automatic/fr3_media.py`` writes it (``joint_N_pos`` radians, the
velocity and effort columns NaN). Every joint angle encodes its source row
(offset per joint + 0.01 * row), so a misaligned or reordered column shows up
as a wrong number, not just a wrong shape.
"""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from levi.conversion import joint_states, raw, registry
from levi.conversion.engine import execute
from levi.conversion.joint_states import GRIPPER_FEATURE, JOINT_FEATURE, Joints
from levi.conversion.options import Options

ROOT = Path(__file__).resolve().parents[1]
ROWS = 12
FPS = 10
OFFSETS = [0.1, -0.5, 0.2, -2.0, 0.3, 2.0, 0.7]  # inside the FR3 limits
CAMERAS = {"wrist_camera": "observation.images.hand"}
# The same source capture through three plans: every Nth row, every row
# (lossless remux), and the static filter (rows 9 and 10 are dropped, so the
# retained rows are not a regular stride).
CONFIGS = {
    "resample": {"fps": 5},
    "retime": {"fps": FPS, "timing": "retime", "filter_static": False},
    "filtered": {"fps": FPS},
}


def options(**changes) -> Options:
    return Options(cameras=CAMERAS, workers=1, **changes)


def build_capture(root: Path, joints=True, demos=("demo_001",)) -> Path:
    """``task/<demo>`` folders. Real captures number frames from 1."""
    task = root / "task"
    first = task / demos[0]
    first.mkdir(parents=True)
    ids = np.arange(1, ROWS + 1)
    t = 1.7e9 + np.arange(ROWS) / FPS
    head = {
        "timestamp_sec": t,
        "frame_index": ids,
        "success_flag": 0,
        "source_stamp_sec": t,
    }
    pd.DataFrame(
        {
            **head,
            # moves for six rows, then stands still
            "px": np.minimum(np.arange(ROWS), 5) * 0.01,
            "py": 0.0,
            "pz": 0.0,
            "qx": 0.0,
            "qy": 0.0,
            "qz": 0.0,
            "qw": 1.0,
        }
    ).to_csv(first / "end_effector_pose.csv", index=False)
    pd.DataFrame(
        {
            **head,
            "finger_left": 0.01,
            "finger_right": 0.01,
            "gripper_width": 0.085 * (1 - np.arange(ROWS) / ROWS),  # metres
            "last_gripper_command": ["open"] * 6 + ["close"] * 6,
        }
    ).to_csv(first / "gripper_state.csv", index=False)
    if joints:
        angles = {
            f"joint_{i + 1}_pos": OFFSETS[i] + 0.01 * np.arange(ROWS) for i in range(7)
        }
        nan = {
            f"joint_{i}_{kind}": np.nan for i in range(1, 8) for kind in ("vel", "eff")
        }
        pd.DataFrame({**head, **angles, **nan}).to_csv(
            first / "robot_joint_states.csv", index=False
        )
    (task / "task_description.txt").write_text("Move the gripper")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=64x48:rate={FPS}:duration={ROWS / FPS}",
            "-frames:v",
            str(ROWS),
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            str(first / "wrist_camera.mp4"),
        ],
        check=True,
    )
    for name in demos[1:]:
        shutil.copytree(first, task / name)
    return root


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def read_json(path: Path):
    return json.loads(path.read_text())


def read_json_lines(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def episode_frame(dataset: Path) -> pd.DataFrame:
    return pd.read_parquet(next((dataset / "data").rglob("*.parquet")))


@pytest.fixture(scope="module")
def converted(tmp_path_factory):
    """One joint-carrying capture converted under each plan."""
    root = tmp_path_factory.mktemp("joints")
    capture = build_capture(root / "capture")
    before = tree_hashes(capture)
    datasets = {
        name: Path(
            execute("pipeline", capture, root / name, options(**plan))["dataset_path"]
        )
        for name, plan in CONFIGS.items()
    }
    assert tree_hashes(capture) == before, "the source capture is read-only"
    return capture, datasets


# --- what is written ----------------------------------------------------------------


@pytest.mark.parametrize("plan", CONFIGS)
def test_joint_columns_follow_the_retained_rows(converted, plan):
    capture, datasets = converted
    dataset = datasets[plan]
    info = read_json(dataset / "meta/info.json")
    assert info["robot_type"] == "franka_fr3"
    features = info["features"]
    assert features[JOINT_FEATURE] == {
        "dtype": "float32",
        "shape": [7],
        "names": [f"joint_{i}" for i in range(1, 8)],
    }
    assert features[GRIPPER_FEATURE] == {
        "dtype": "float32",
        "shape": [1],
        "names": ["gripper_width"],
    }
    # the pose-based columns are untouched
    assert features["observation.state"]["names"][-1] == "gripper"
    assert features["action"]["shape"] == [7]

    frame = episode_frame(dataset)
    provenance = read_json_lines(dataset / "meta/levi_provenance.jsonl")[0]
    rows = np.array(provenance["source_positions"])
    n = len(rows)
    assert len(frame) == n
    if plan == "retime":
        assert rows.tolist() == list(range(ROWS))
    if plan == "filtered":
        assert 9 not in rows and 10 not in rows and n < ROWS  # not a regular stride
    # timestamps stay frame_index / fps (the viewer and validation rely on it)
    assert np.allclose(frame.timestamp, np.arange(n) / info["fps"], atol=1e-6, rtol=0)

    joints = np.stack(frame[JOINT_FEATURE])
    assert joints.dtype == np.float32 and joints.shape == (n, 7)
    source = pd.read_csv(capture / "task/demo_001/robot_joint_states.csv")
    expected = source[joint_states.POSITION_COLUMNS].to_numpy()[rows]
    assert np.allclose(joints, expected, atol=1e-6, rtol=0)
    # the row a value comes from is the captured frame, recovered independently
    # of the pose file: joint_1 = offset + 0.01 * (frame_index - 1)
    recovered = np.rint((joints[:, 0] - OFFSETS[0]) / 0.01).astype(int)
    assert (recovered == np.array(provenance["source_frame_ids"]) - 1).all()
    # each column keeps its own joint (the offsets differ per joint)
    assert np.allclose(joints - 0.01 * rows[:, None], OFFSETS, atol=1e-6, rtol=0)

    stats = read_json(dataset / "meta/stats.json")
    assert np.allclose(stats[JOINT_FEATURE]["min"], joints.min(0), atol=1e-6)
    assert np.allclose(stats[JOINT_FEATURE]["max"], joints.max(0), atol=1e-6)
    assert stats[JOINT_FEATURE]["count"] == [n]
    assert len(stats[GRIPPER_FEATURE]["min"]) == 1
    per_episode = read_json_lines(dataset / "meta/episodes_stats.jsonl")[0]["stats"]
    assert JOINT_FEATURE in per_episode and GRIPPER_FEATURE in per_episode
    schema = pq.read_schema(next((dataset / "data").rglob("*.parquet")))
    listed = schema.field(JOINT_FEATURE).type
    assert pa.types.is_list(listed) and listed.value_type == pa.float32()
    assert schema.field(GRIPPER_FEATURE).type == pa.float32()


@pytest.mark.parametrize("plan", CONFIGS)
def test_gripper_width_is_the_measured_aperture_in_metres(converted, plan):
    capture, datasets = converted
    dataset = datasets[plan]
    rows = np.array(
        read_json_lines(dataset / "meta/levi_provenance.jsonl")[0]["source_positions"]
    )
    frame = episode_frame(dataset)
    csv = pd.read_csv(capture / "task/demo_001/gripper_state.csv")
    width = frame[GRIPPER_FEATURE].to_numpy()
    # the CSV value, unscaled: metres (0.085 is a fully open Robotiq 2F-85)
    assert np.allclose(width, csv.gripper_width.to_numpy()[rows], atol=1e-7, rtol=0)
    assert width.max() == pytest.approx(0.085) and 0 <= width.min()
    # the gripper channel of state / action is still the open=1 / close=0 command
    command = np.stack(frame["observation.state"])[:, -1]
    assert command.tolist() == (rows < 6).astype(float).tolist()
    assert set(np.stack(frame["action"])[:, -1]) <= {0.0, 1.0}
    record = read_json(dataset / "meta/levi_conversion.json")
    assert record["gripper"].startswith("command: open=1, close=0")
    assert record["joint_positions"]["unit"] == "radians"
    assert record["joint_positions"]["gripper_width"]["unit"] == "metres"
    assert record["joint_positions"]["robot_type"] == "franka_fr3"
    assert record["options"]["export_joints"] is True


# --- the viewer's rule (src/lib/replay/fr3-columns.ts) -------------------------------


def _ts_constants():
    path = ROOT / "src/lib/replay/fr3-columns.ts"
    constants = ROOT / "src/utils/constants.ts"
    if not (path.is_file() and constants.is_file()):
        pytest.skip("the viewer sources are not in this checkout")
    text = path.read_text()

    def regex(name):
        found = re.search(rf"const {name} =\s*/(.+)/([a-z]*);", text)
        assert found, f"{name} not found in fr3-columns.ts: update this test"
        return re.compile(found[1], re.IGNORECASE if "i" in found[2] else 0)

    def strings(name):
        found = re.search(
            rf"const {name}\s*=\s*(?:new Set\()?\[(.*?)\]", text, re.DOTALL
        )
        assert found, f"{name} not found in fr3-columns.ts: update this test"
        return re.findall(r'"([^"]*)"', found[1])

    delimiter = re.search(r'SERIES_NAME_DELIMITER:\s*"([^"]*)"', constants.read_text())
    assert delimiter
    return {
        "joint": regex("JOINT_RE"),
        "gripper": regex("GRIPPER_RE"),
        "family": strings("FR3_FAMILY"),
        "generic": strings("GENERIC_ROBOT_TYPES"),
        "priority": strings("STATE_FEATURE_PRIORITY"),
        "delimiter": delimiter[1],
    }


def flat_keys(info: dict, delimiter: str) -> list[str]:
    """The keys the episode viewer builds from info.json (fetch-data.ts: one per
    name, or per index when a feature has no names)."""
    keys = ["timestamp"]
    skip = {"timestamp", "frame_index", "episode_index", "index", "task_index"}
    for key, feature in info["features"].items():
        if feature["dtype"] == "video" or len(feature["shape"]) != 1 or key in skip:
            continue
        names = feature.get("names")
        labels = names if isinstance(names, list) else range(feature["shape"][0])
        keys += [f"{key}{delimiter}{label}" for label in labels]
    return keys


def detect(rule: dict, robot_type: str, keys: list[str]):
    """``detectFr3Replay`` restated: (mode, state feature, state columns)."""
    lower = robot_type.strip().lower()
    fr3 = any(word in lower for word in rule["family"])
    if not fr3 and lower not in rule["generic"]:
        return "none", None, []
    features: dict[str, list[tuple[str | None, str]]] = {}
    for key in keys:
        feature, found, name = key.partition(rule["delimiter"])
        features.setdefault(feature, []).append((name if found else None, key))
    ordered = [f for f in rule["priority"] if f in features] + sorted(
        f
        for f in features
        if f.startswith("observation.") and f not in rule["priority"]
    )
    if fr3:
        for feature in ordered:
            by_index: dict[int, str] = {}
            for name, key in features[feature]:
                match = rule["joint"].match(name) if name else None
                if match:
                    by_index.setdefault(int(match[1]), key)
            index = sorted(by_index)
            if len(index) == 7 and index[0] in (0, 1) and index[-1] == index[0] + 6:
                return "joints", feature, [by_index[i] for i in index]
    for feature in ordered:
        names = {name.lower() for name, _ in features[feature] if name}
        if {"x", "y", "z"} <= names and (
            {"rx", "ry", "rz"} <= names or {"qx", "qy", "qz", "qw"} <= names
        ):
            return "pose", feature, []
    return "none", None, []


def test_exported_columns_are_recognised_as_fr3_joints(converted):
    rule = _ts_constants()
    _, datasets = converted
    info = read_json(datasets["filtered"] / "meta/info.json")
    keys = flat_keys(info, rule["delimiter"])
    mode, feature, columns = detect(rule, info["robot_type"], keys)
    assert (mode, feature) == ("joints", JOINT_FEATURE)
    assert columns == [
        f"{JOINT_FEATURE} | joint_{i}" for i in range(1, 8)
    ]  # joint order
    # the measured width has a name the viewer's gripper rule accepts
    assert rule["gripper"].match("gripper_width")
    assert f"{GRIPPER_FEATURE} | gripper_width" in keys
    # without the FR3 robot_type the very same columns stay a pose dataset: the
    # reason an exported dataset says franka_fr3 instead of generic_arm
    assert detect(rule, "generic_arm", keys)[0] == "pose"
    assert any(word in info["robot_type"] for word in rule["family"])


def test_the_viewer_rule_reads_what_the_pipeline_names_without_joints(tmp_path):
    rule = _ts_constants()
    capture = build_capture(tmp_path / "plain", joints=False)
    result = execute("pipeline", capture, tmp_path / "out", options())
    info = read_json(Path(result["dataset_path"]) / "meta/info.json")
    assert info["robot_type"] == "generic_arm"
    mode, feature, _ = detect(
        rule, info["robot_type"], flat_keys(info, rule["delimiter"])
    )
    assert (mode, feature) == ("pose", "observation.state")


def test_joint_limits_in_the_model_leave_room_for_the_radian_guard():
    # joint_states refuses |angle| > 2*pi as "not radians"; every FR3 limit
    # in the model the viewer draws (public/robots/fr3/fr3.urdf) is below it.
    urdf = ROOT / "public/robots/fr3/fr3.urdf"
    if not urdf.is_file():
        pytest.skip("the FR3 model is not in this checkout")
    limits = [
        float(v)
        for pair in re.findall(r'lower="([-\d.]+)" upper="([-\d.]+)"', urdf.read_text())
        for v in pair
    ]
    assert len(limits) >= 14
    assert max(abs(v) for v in limits) < joint_states.MAX_ABS_RADIANS


# --- no joint source: the output is what it was ---------------------------------------


def test_no_joint_source_gives_the_same_dataset_as_before(tmp_path):
    plain = build_capture(tmp_path / "plain", joints=False)
    with_file = build_capture(tmp_path / "with", joints=True)
    default = Path(
        execute("pipeline", plain, tmp_path / "default", options())["dataset_path"]
    )
    off = Path(
        execute("pipeline", plain, tmp_path / "off", options(export_joints=False))[
            "dataset_path"
        ]
    )
    ignored = Path(
        execute(
            "pipeline", with_file, tmp_path / "ignored", options(export_joints=False)
        )["dataset_path"]
    )
    info = read_json(default / "meta/info.json")
    assert info["robot_type"] == "generic_arm"
    assert not [k for k in info["features"] if "joint" in k or "gripper_width" in k]
    assert "joint_positions" not in read_json(default / "meta/levi_conversion.json")
    # Left at its default with nothing to export, the record has no new key.
    assert (
        "export_joints"
        not in read_json(default / "meta/levi_conversion.json")["options"]
    )
    # A joint file that is switched off is ignored: data, videos and statistics
    # are byte for byte those of a capture without the file.
    for other in (off, ignored):
        for name in tree_hashes(default):
            if name in ("meta/levi_conversion.json", "meta/levi_preflight.json"):
                continue  # these record the source path and the options
            assert (other / name).read_bytes() == (default / name).read_bytes(), name
    assert (
        read_json(off / "meta/levi_conversion.json")["options"]["export_joints"]
        is False
    )


def test_the_option_defaults_on_and_has_an_off_switch():
    assert Options().export_joints is True
    assert Options(export_joints=False).export_joints is False
    assert registry.output_format("lerobot_v21").joint_columns is True
    assert registry.output_format("recap_value").joint_columns is False


def test_recap_and_browsing_views_never_carry_joint_columns(tmp_path):
    capture = build_capture(tmp_path / "capture")
    recap = Path(
        execute(
            "pipeline",
            capture,
            tmp_path / "recap",
            registry.with_defaults(
                options(target_options={"dataset_type": "sft"}), "recap_value"
            ),
        )["dataset_path"]
    )
    view = Path(execute("view", capture, tmp_path / "view", options())["dataset_path"])
    for dataset in (recap, view):
        info = read_json(dataset / "meta/info.json")
        assert info["robot_type"] == "generic_arm"
        assert not [k for k in info["features"] if "joint" in k or "gripper_width" in k]
        assert "joint_positions" not in read_json(dataset / "meta/levi_conversion.json")
        assert "observation.joint_positions" not in episode_frame(dataset).columns


# --- refusals: nothing is filled, interpolated or zeroed -----------------------------


def mutate(demo: Path, change, name="robot_joint_states.csv"):
    path = demo / name
    frame = pd.read_csv(path)
    result = change(frame)
    (frame if result is None else result).to_csv(path, index=False)


def _set(column, row, value):
    def change(frame):
        frame[column] = frame[column].astype(object)
        frame.loc[row, column] = value

    return change


REFUSED = {
    "nan": (
        _set("joint_4_pos", 3, np.nan),
        r"1 joint angle\(s\) are missing or not finite \(first: row 3, joint_4_pos\)",
    ),
    "text": (_set("joint_2_pos", 5, "n/a"), r"row 5, joint_2_pos"),
    "inf": (_set("joint_7_pos", 0, np.inf), r"row 0, joint_7_pos"),
    "missing_rows": (
        lambda f: f.iloc[:-1],
        r"11 rows but end_effector_pose.csv has 12",
    ),
    "extra_rows": (
        lambda f: pd.concat([f, f.iloc[[-1]]]),
        r"13 rows but end_effector_pose.csv has 12",
    ),
    "no_frame_index": (
        lambda f: f.drop(columns="frame_index"),
        r"no frame_index column",
    ),
    "shifted_ids": (
        lambda f: f.assign(frame_index=f.frame_index + 1),
        r"frame IDs differ",
    ),
    "shifted_time": (
        lambda f: f.assign(timestamp_sec=f.timestamp_sec + 5),
        r"timestamps differ",
    ),
    "one_joint_missing": (lambda f: f.drop(columns="joint_7_pos"), r"not joint_7_pos"),
    "degrees": (
        lambda f: f.assign(joint_2_pos=np.degrees(f.joint_2_pos)),
        r"must be radians",
    ),
}


@pytest.mark.parametrize("case", REFUSED)
def test_an_unusable_joint_file_is_refused_with_its_reason(tmp_path, case):
    change, reason = REFUSED[case]
    capture = build_capture(tmp_path / "capture", joints=True)
    demo = capture / "task/demo_001"
    pose, grip, *_ = raw.load(demo)  # a clean capture; the file is damaged after
    mutate(demo, change)
    with pytest.raises(raw.CaptureError, match=reason) as caught:
        joint_states.read(demo, pose, grip)
    assert caught.value.code == "joint_states"
    assert "robot_joint_states.csv" in str(caught.value)


def test_no_joint_columns_or_no_file_is_no_source_not_an_error(tmp_path):
    capture = build_capture(tmp_path / "capture", joints=True)
    demo = capture / "task/demo_001"
    pose, grip, *_ = raw.load(demo)
    assert isinstance(joint_states.read(demo, pose, grip), Joints)
    # another layout (no joint_N_pos column at all): not ours, not read
    mutate(demo, lambda f: f[["timestamp_sec", "frame_index", "joint_1_vel"]])
    assert joint_states.read(demo, pose, grip) is None
    (demo / "robot_joint_states.csv").unlink()
    assert joint_states.read(demo, pose, grip) is None


def test_a_refused_joint_file_stops_the_conversion_before_anything_is_written(tmp_path):
    capture = build_capture(tmp_path / "capture", joints=True)
    mutate(capture / "task/demo_001", _set("joint_4_pos", 3, np.nan))
    target = tmp_path / "refused"
    with pytest.raises(
        ValueError,
        match=r"preflight failed.*robot_joint_states\.csv.*row 3, joint_4_pos",
    ):
        execute("pipeline", capture, target, options())
    assert not target.exists() and not (tmp_path / ".refused.partial").exists()
    # the documented way out: turn the export off, the capture converts as before
    result = execute(
        "pipeline", capture, tmp_path / "off", options(export_joints=False)
    )
    info = read_json(Path(result["dataset_path"]) / "meta/info.json")
    assert info["robot_type"] == "generic_arm"
    assert JOINT_FEATURE not in info["features"]


def test_a_misaligned_joint_file_is_refused_even_with_the_export_off(tmp_path):
    # Not new: any extra CSV with a frame_index must match the pose rows.
    capture = build_capture(tmp_path / "capture", joints=True)
    mutate(capture / "task/demo_001", lambda f: f.assign(frame_index=f.frame_index + 1))
    with pytest.raises(ValueError, match="alignment"):
        execute("pipeline", capture, tmp_path / "x", options(export_joints=False))


def test_joint_source_in_only_some_episodes_is_refused(tmp_path):
    capture = build_capture(tmp_path / "capture", demos=("demo_001", "demo_002"))
    (capture / "task/demo_002/robot_joint_states.csv").unlink()
    with pytest.raises(
        ValueError, match=r"present in 1 of 2 episodes but missing .*task/demo_002"
    ):
        execute("pipeline", capture, tmp_path / "mixed", options())
    assert not (tmp_path / "mixed").exists()
    # without joints anywhere, or with the export off, the capture converts
    execute("pipeline", capture, tmp_path / "off", options(export_joints=False))


def test_settle_decides_one_layout_for_all_episodes():
    angles = np.zeros((3, 7))
    good = Joints(angles, np.full(3, 0.04))
    bad = Joints(angles, None, "gripper_state.csv gripper_width spans 1..85")
    assert joint_states.settle([("a", None), ("b", None)]) is None
    assert joint_states.settle([("a", good), ("b", good)]).gripper_width is True
    layout = joint_states.settle([("a", good), ("b", bad)])
    assert (
        layout.gripper_width is False
        and "gripper_width spans" in layout.gripper_problem
    )
    with pytest.raises(ValueError, match="1 of 2 episodes"):
        joint_states.settle([("a", good), ("b", None)])


def test_an_implausible_gripper_width_is_not_exported_and_says_so(tmp_path):
    capture = build_capture(tmp_path / "capture", joints=True)
    demo = capture / "task/demo_001"
    pose, grip, *_ = raw.load(demo)
    # millimetres (the Robotiq's stroke, 85, instead of 0.085 m)
    millimetres = joint_states.read(
        demo, pose, grip.assign(gripper_width=grip.gripper_width * 1000)
    )
    assert (
        millimetres.gripper_width is None
        and "not metres" in millimetres.gripper_problem
    )
    gap = grip.copy()
    gap.loc[2, "gripper_width"] = np.nan
    missing = joint_states.read(demo, pose, gap)
    assert missing.gripper_width is None and "row 2" in missing.gripper_problem
    assert missing.positions.shape == (ROWS, 7)  # the joint angles are unaffected

    mutate(
        demo,
        lambda f: f.assign(gripper_width=f.gripper_width * 1000),
        "gripper_state.csv",
    )
    result = execute("pipeline", capture, tmp_path / "out", options())
    dataset = Path(result["dataset_path"])
    info = read_json(dataset / "meta/info.json")
    assert JOINT_FEATURE in info["features"] and GRIPPER_FEATURE not in info["features"]
    assert info["robot_type"] == "franka_fr3"
    gripper = read_json(dataset / "meta/levi_conversion.json")["joint_positions"][
        "gripper_width"
    ]
    assert gripper["exported"] is False and "not metres" in gripper["reason"]
    assert GRIPPER_FEATURE not in episode_frame(dataset).columns
