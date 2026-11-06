# Built-in conversion / 内置转换

LEVI includes the complete capture workflow. Everything runs in its uv environment through the Workbench page or `uv run levi convert`; no external scripts or training framework are required.

The workflow is **inspect → choose an export → run**:

1. **Inspect** (`convert/inspect` job, CSV + ffprobe, no decode — 2 s for 171 real demos): detects the input format, lists every requirement with its status (pass / warning / fail / *checked while converting*), and rates every export target: supported, supported with warnings, or unsupported — with the reasons and one-click fixes (exclude failing episodes, label outcomes, export as demonstrations…).
2. **Choose an export** and its options (timing, FPS, filtering, workers; target options such as RECAP's failure reward).
3. **Run**: a job with live progress (stage stepper, bar, current episode, elapsed/ETA). The dataset is written to a hidden staging directory and renamed into place only if every episode passed the preflight and the dataset validated. Sources are never modified.

## 格式 / Supported formats

The registry (`levi/conversion/registry.py`) is the source of truth; `GET /api/levi/convert/formats` returns it and the Workbench shows it under "Supported formats". A test keeps this table in sync with the registry.

| Input | Id | Exports to | Evidence |
| --- | --- | --- | --- |
| Robot capture — pose/gripper CSV + video demos (teleoperation `teleop` or `policy_rollout` variant) | `robot_capture` | `lerobot_v21`, `recap_value` | real data: 171-demo screws eval batch, `data_collection_robotiq` teleop demos |
| Robot capture — CSV + image folders | `image_sequence` | `lerobot_v21`, `recap_value` | fixture only |
| DROID raw — HDF5, metadata and three MP4s | `droid_raw` | browsing/annotation view only; training export needs explicit mapping | 500-demo raw sample; CPU fixture |
| LeRobot dataset v2.x (re-export) | `lerobot` | `recap_value` | real data (screws conversion) |

| Output | Id | Notes |
| --- | --- | --- |
| LeRobot v2.1 | `lerobot_v21` | per-episode parquet + H.264 MP4, loadable by LeRobot, openpi and the LEVI viewer |
| RECAP value dataset (π\*0.6) | `recap_value` | LeRobot v2.1 + rewards, terminal success flags and RLinf's returns sidecar — see [RECAP.md](RECAP.md) |

Known gaps (listed in the UI with reason and workaround): LeRobot v3 as conversion **output** (write v2.1, then use lerobot's `convert_dataset_v21_to_v30`), LeRobot v3 as conversion **input** (export from the raw capture, or convert v3→v2.1), RLDS / Open X-Embodiment and non-DROID HDF5 profiles (no schema reader yet — see "Adding a format"). v3 datasets can still be browsed, annotated and validated.

## DROID raw browsing view

Install the optional HDF5 reader with `uv sync --locked --extra droid`. Register the folder containing `demo_0000/`, `demo_0001/`, and so on. LEVI recognizes `trajectory.h5`, the metadata JSON and the three videos in `recordings/MP4/`; it builds a derived, video-aligned v2.1 browsing view under the workspace and leaves every raw file untouched. The viewer and the Agent Harness can annotate this view. Its state/action fields are the recorded seven joint positions plus gripper positions, not inferred commands.

The raw MP4s commonly declare 60 FPS while control samples are near 14 Hz. The view stream-copies H.264 packets at a declared 14.3 FPS, uses the video-frame count shared by all cameras, and records the original per-row control timestamps and any trimmed trailing rows in `meta/levi_provenance.jsonl`. This is a uniform viewing clock, not exact capture time; `max_source_to_view_error_seconds` identifies episodes whose time boundaries need manual review. Native comparisons must use the same view-clock rule. Camera resolution may vary by episode; actual sizes are recorded in `meta/levi_droid_video_shapes.jsonl`. The view does not imply a generic DROID-to-training-data writer. Episodes without `current_task` remain browsable as `Unspecified task` and carry an explicit `levi_task_unspecified` marker; episode-level success/failure is never used to invent task text.

## 输入 / Capture schema (`robot_capture`, `image_sequence`)

```text
captures/session-a/
└── task-a/
    ├── task_description.txt
    └── demo_001/
        ├── end_effector_pose.csv
        ├── gripper_state.csv
        ├── robot_joint_states.csv  (optional: joint angles, see "Joint angles")
        ├── events.csv          (optional)
        ├── metadata.json       (optional)
        ├── wrist_camera.mp4    (or wrist_camera_raw.avi, or wrist_camera/ image folder)
        └── side_camera.mp4
```

- Pose columns: `timestamp_sec, frame_index, success_flag, source_stamp_sec, px, py, pz, qx, qy, qz, qw`.
- Gripper columns: `timestamp_sec, frame_index, success_flag, source_stamp_sec, finger_left, finger_right, gripper_width, last_gripper_command`. Extra columns are read but ignored.
- Joint columns (optional file `robot_joint_states.csv`): `timestamp_sec, frame_index, success_flag, source_stamp_sec, joint_1_pos` .. `joint_7_pos` (radians), plus `joint_N_vel` / `joint_N_eff` (read by nothing here; the FR3 collector writes NaN). See "Joint angles and measured gripper width" below.
- Quaternion order XYZW; metres; seconds; commands `open` / `close` only.
- `success_flag` must be **consistent** within one demo (a mix of 0 and 1 means a bad merge). Teleoperation leaves it at 0; policy rollouts set it to the outcome.
- `metadata.json["data_source"] == "policy_rollout"` marks an eval/RL rollout (variant `policy_rollout`); its `eval.outcome` becomes the episode's `levi_outcome`. Teleoperation omits `data_source` and has no outcome.
- Camera stalls: flat `camera_stalled` (teleop) or `cameras.stall_detection.stalled` (rollout). Completion: `stop_demo` (teleop) or `episode_end` (rollout) in `events.csv`, plus `stopped_at` — required only with `require_complete`.
- Every CSV row is the same ordered video/image frame; frame ids unique, integer, increasing (real captures start at 1); pose/gripper ids and times agree. `discarded_*` folders are ignored.
- Image folders hold naturally sorted PNG/JPEG frames declared at `source_fps`.
- Task text: `task_description.txt` one level above the demos, else the folder name; `task_map` maps text once (it is not chained).

Requirements needing a full decode — frozen camera, decoded frame count, duplicate first frames — are shown as *checked while converting*, never as passed; a failure there stops the job before anything is published.

## 帧时间 / Timing modes

| `timing` | Rows kept | Video | Use |
| --- | --- | --- | --- |
| `resample` (default for `lerobot_v21`) | every `source_fps / fps`-th row; `fps` is lowered automatically to the slowest measured camera (reported as `fps_note`) | one decode + one H.264 encode | uniform rate for imitation learning |
| `retime` (default for `recap_value`, browsing views) | **every** captured row, declared at `fps` | lossless stream copy: timestamps rewritten to exactly `i / fps`, pixels bit-identical, no encode | one row per executed action (RECAP rewards), fastest |

`retime` changes playback speed slightly when `fps` differs from the measured rate (e.g. 9.47 Hz captured, declared 10); the inspection shows the measured range. The retime is exact: the stream is first copied into a time base where both frame durations are whole ticks (`lcm` of the two rate numerators), then rescaled; the result is verified from packet timestamps and re-encoded if the source was not constant-frame-rate. `filter_static` (drop frames while the robot is still) applies in either mode and is off by default for RECAP.

Timestamps are always `frame_index / fps` — the viewer, SAM3 and validation rely on it.

## 性能 / Performance

The parent process plans every episode from its CSVs once (retained rows, state/action, tasks, provenance) and writes the parquet files; a `spawn` process pool (`workers`, default `min(4, cpus/4)`, OpenCV single-threaded per worker) handles one camera of one episode at a time:

- `encode`: one source decode that simultaneously feeds the preflight scan (decoded count, frozen span, first-frame hash) and the single H.264 encode, then one decode of the output for its RGB statistics — 2 decodes + 1 encode per camera (the old stage chain did 9 decodes + 3 encodes).
- `remux` (`retime`, H.264 source): stream copy, then one output decode that provides both preflight and statistics — 1 decode, 0 encodes (~0.1 s per real 200-frame video for the copy).

Validation reuses those measurements instead of decoding a third time. `keep_intermediates` additionally writes the old stage-by-stage audit copies (`staged/`, `filtered/`) to `outputs/LEVI/workbench/jobs/<timestamp>/intermediate/`.

## 配置 / Options

```json
{
  "fps": 10,
  "source_fps": 30,
  "timing": "resample",
  "filter_static": true,
  "workers": null,
  "keep_intermediates": false,
  "target": "lerobot_v21",
  "target_options": {},
  "cameras": {
    "wrist_camera": "observation.images.hand",
    "side_camera": "observation.images.view1"
  },
  "xyz_threshold": 0.005,
  "rotation_threshold": 0.01,
  "gripper_margin": 2,
  "frozen_threshold": 0.5,
  "stale_run": 3,
  "require_complete": false,
  "orientation": "euler",
  "action_mode": "next_state",
  "robot_type": "generic_arm",
  "export_joints": true,
  "task_map": {"pick_object": "Pick up the object."},
  "exclude_demos": ["task-a/demo_004"],
  "outcome_labels": {}
}
```

A target's defaults (RECAP: `timing=retime`, `filter_static=false`) apply unless the option is set explicitly. `outcome_labels` is filled automatically at plan time from human labels (see below). Unknown excluded paths are rejected.

```bash
uv run levi convert inspect  --source captures/session-a --output unused
uv run levi convert pipeline --source captures/session-a --output run-a \
  --options configs/conversion.json
uv run levi convert validate --source run-a --output unused
```

`--output` is any new path inside `LEVI_WORKSPACE`. Left blank in the Workbench, it is `<source name>_<lerobot|recap>_<timestamp>` directly under the workspace. The dataset is the output directory itself (`meta/`, `data/`, `videos/`), with its reports in `meta/levi_preflight.json` and `meta/levi_validation.json`.

Threshold semantics: `xyz_threshold` metres and `rotation_threshold` radians (quaternion geodesic) of motion since the **last retained** frame; `gripper_margin` protects ±N frames around command changes; `frozen_threshold` is the mean absolute difference from the first 32×32 thumbnail (0–255; 0 disables); `stale_run` is the tolerated run of repeated robot timestamps (judged on the rows the dataset will contain); `orientation` `euler` (unwrapped XYZ) or `quaternion` (sign-continuous, state width 8); `action_mode` `next_state` or `state`.

### Single stages (advanced)

The older stage-by-stage commands remain for audits and repairs: `summary`, `images`, `fps-preview`, `fps`, `frozen`, `stage-preview`, `stage`, `static`, `filter-preview`, `filter`, `convert` (an already-staged capture → v2.1; every row kept, FPS must match), `timestamps`, `tasks-preview`, `tasks`, `validate`. Read-only stages print JSON and create no dataset.

## 浏览视图与标注迁移 / Raw captures: browse, annotate, carry over

Registering a raw capture (Workbench → Local datasets, or `POST /api/levi/catalog`) builds a **browsing view** in the background: every frame, videos retimed by stream copy to the median measured rate, parquet from the CSVs, `meta/levi_view.json` (source, input format, fps, excluded episodes). Episodes that fail inspection are left out and listed instead of blocking the capture. The viewer, language/event annotation, SAM3 and outcome labels then work on the raw capture; they are stored under the raw dataset's own name. A view cannot be exported ("convert first — annotations carry over"). Re-registering a changed capture rebuilds the view and re-keys existing annotations by demo; files of demos that disappeared move to `orphaned/`.

When a conversion finishes, annotations made on its source (a raw view or a LeRobot dataset) are **carried over** (`levi/annotations/carryover.py`), matched by `source_demo` and the provenance frame ledgers — never by episode index:

- language atoms move to the nearest retained frame (event atoms stay on frames; persistent atoms collapsing onto one frame are counted);
- outcome labels are copied;
- SAM3 masks are kept only on frames retained exactly, published as a new revision (`model.provider = carry_over`).

Nothing already present in the output is overwritten; the report is `meta/levi_annotation_carryover.json` and appears in the job record.

The Workbench's dataset list shows each entry's **format & version** (`levi/describe.py`, returned as `format` by `GET /api/levi/catalog`): *Raw capture* (input format, rollout/teleop, browsing view fps, left-out episodes), *LeRobot vX · LEVI conversion* (source format, resample/retime, fps, static filtering), *LeRobot vX · RECAP* (dataset type, outcome counts, failure reward), *annotated export*, or *registered as-is*. The same record carries a capability table; on a raw capture the viewer shows a banner, keeps browsing, statistics, filtering, frame gallery, action insights, annotation, SAM3, outcome labels and review flags, labels Doctor results as checks of the view, and replaces "export annotated dataset" with a note linking to the Workbench (the page opens with the capture pre-filled). A view decodes nothing: videos are only remuxed (171 real demos / 342 videos in 38 s), so it carries no pixel statistics.

## 结局标签 / Episode outcome labels

The sidebar dot next to each episode shows its outcome: metadata (`levi_outcome`) as a plain dot, human labels with a ring. Clicking cycles the human label success → failure → cleared. Labels are one file per episode (`outputs/LEVI/workbench/annotations/<name>/outcomes/episode_NNNNNN.json`), so collaborators don't overwrite each other; they override metadata, feed the Failures filter, are snapshotted into conversion plans (`outcome_labels`), and are written into annotated exports as `levi_outcome` + `levi_outcome_source: "human"`.

## 输出与交接 / Output and review handoff

`meta/levi_provenance.jsonl`: `episode_index`, `source_demo`, `source_positions` (0-based raw rows kept), `source_frame_ids`, `source_capture_timestamps`. `meta/episodes.jsonl` rows carry `source_demo` and, for rollouts, `levi_outcome` (+ `levi_outcome_source` for human labels). `meta/levi_conversion.json` (schema `levi.conversion.v2`) records the input format/variant, target, options, fps, timing and how many videos were remuxed or encoded. State is `[x,y,z,rx,ry,rz,gripper_command]` (open=1/close=0, not measured width); next-state action repeats the final state. When the capture has joint angles, two more columns are written next to them and `meta/levi_conversion.json` gets a `joint_positions` block (below). Statistics use all decoded output pixels (RGB in [0,1]).

Use provenance to map flagged episodes back to capture paths and configure `exclude_demos`; never assume episode N equals `demo_N`. Exit codes: 0 ok; 2 a completed report contains failing checks; 1 execution/configuration/preflight error.

## 关节角与实测夹爪 / Joint angles and measured gripper width

A conversion used to keep only the end-effector pose, so a converted dataset could not drive a 3D replay of the arm. When the capture holds the measured joint angles, `convert pipeline` into `lerobot_v21` now also writes them (`levi/conversion/joint_states.py`):

| Written | Source | Notes |
| --- | --- | --- |
| `observation.joint_positions` | `robot_joint_states.csv`, `joint_1_pos` .. `joint_7_pos` | float32, shape `[7]`, names `joint_1` .. `joint_7`, **radians**, the same source row as the pose |
| `observation.gripper_width` | `gripper_state.csv`, `gripper_width` | float32, shape `[1]`, **metres**, the *measured* aperture (0.085 is a fully open Robotiq 2F-85) |
| `info.json` `robot_type` | — | `franka_fr3` when `robot_type` is still the default `generic_arm`; any other value you set is kept |

- The existing columns do not change. `observation.state` and `action` keep `[x,y,z,rx,ry,rz,gripper]`, where `gripper` is still the open=1 / close=0 **command**; the measured width is a separate column and never replaces it.
- Rows are matched one to one with the pose file and kept at exactly the retained positions (`resample`, `retime` and the static filter all apply), so `timestamp` stays `frame_index / fps`. Nothing is interpolated.
- Why `robot_type` changes: the viewer draws an FR3 from joint columns only when the robot type names the Franka family (`fr3`, `franka`, `panda`); other 7-joint arms also name their joints `joint_1..7` and must not be drawn as an FR3. The names and this rule are read by `src/lib/replay/fr3-columns.ts`; `tests/test_conversion_joints.py` re-derives the rule from that file.
- Switch: `export_joints` (default `true`: export when the source has them). `false` converts exactly as before. A capture without `robot_joint_states.csv` (or with one that has no `joint_N_pos` column at all, another layout) is converted as before in either case: same files, byte for byte, and the `options` record in `meta/levi_conversion.json` has no new key. Scope: `lerobot_v21` through `convert pipeline` / the Workbench. RECAP value exports, browsing views of raw captures and training-pool exports do not carry these columns.
- Refused, with the demo, the reason and the two ways out (set `export_joints` to `false`, or leave the demo out with `exclude_demos`) in the error, before anything is written (nothing is filled, interpolated or set to zero): a joint file with only some of the seven `joint_N_pos` columns; a different row count, `frame_index` or `timestamp_sec` than `end_effector_pose.csv` (a missing `frame_index` column too); a missing, non-numeric or non-finite angle; an angle beyond one turn (`|q| > 2π`: degrees or ticks, not radians); a joint file in only some of the selected demos (a dataset has one set of columns; exclude those demos or set `export_joints` to `false`).
- The measured width is optional: if `gripper_width` is missing, non-finite or outside −0.005..0.2 m in any demo (millimetres, a 0..1 fraction), the joint angles are still written, `observation.gripper_width` is not, a warning is shown and `joint_positions.gripper_width` in `meta/levi_conversion.json` says why. LEVI cannot tell a sensor that was never read from a gripper that stayed closed; the value is exported as recorded.
- **Older datasets** converted before this change have none of these columns and keep `generic_arm`; they get them only by converting the raw capture again into a **new** output directory (a conversion never writes into an existing dataset). Frozen test sets and gold-labelled datasets are not re-converted: their content hashes and locks identify exactly the files that were evaluated or labelled, so a re-conversion would be a different dataset, not the same one with more columns.

中文说明：转换原来只导出末端位姿，转换后的数据集无法驱动机械臂的三维回放。现在 `convert pipeline`（输出 `lerobot_v21`）在原始采集里有 `robot_joint_states.csv`（`joint_1_pos`..`joint_7_pos`，弧度）时，多写一列 `observation.joint_positions`（7 维，弧度，名字 `joint_1`..`joint_7`）；`gripper_state.csv` 的 `gripper_width`（米，实测开度）另写成 `observation.gripper_width`，不替换 `observation.state` / `action` 里的开合指令。`robot_type` 保持默认 `generic_arm` 时改写为 `franka_fr3`（查看器只对 FR3/Franka/Panda 家族用关节列驱动手臂）。开关是选项 `export_joints`（默认 `true`：源里有就导出；`false` 与原来完全一样）。没有关节文件的采集输出与原来逐字节一致。关节文件只含部分列、行数或帧号对不上、有缺失/非数/超过一圈的角度、或只有部分 demo 有关节文件时，转换在写任何东西之前拒绝，说明原因并给出两条出路（`export_joints` 设为 `false`，或用 `exclude_demos` 排除该 demo），不补零、不插值；实测开度不合理时只跳过开度列并记录原因。已有数据集要有这些列，只能重新转换原始采集到新的输出目录；冻结测试集和金标准数据集不重转（哈希和锁定对应的是当时评测或标注的那份文件，重转后就是另一个数据集）。RECAP 导出、浏览视图和训练池导出不带这些列。

## 新增格式 / Adding a format

- **Input**: subclass `InputFormat` (`levi/conversion/inputs/base.py`) with `detect` (cheap confidence 0–1), `inspect` (requirements via `report.Requirement`, per-episode findings, summary) and `episodes` (`SourceEpisode` records: id, media paths, per-camera fps, task, outcome). CSV-and-video captures can usually subclass `RobotCapture`.
- **Output**: subclass `OutputFormat` (usually `LeRobotV21`): `check` (supported / warnings / unsupported with reasons and `Solution`s), `defaults`, an `options_model`, and the writer hooks `episode_columns`, `episode_fields`, `finalize`; `from_dataset` if it can be produced from an existing dataset.
- Register it in `registry.py` and add a fixture builder to `FIXTURES` in `tests/test_formats.py`: the contract tests then check detection, the report and a validated round trip through every compatible pair.

## 限制 / Known limits

- Source fingerprints (size, mtime, per-file stat) detect ordinary recording edits; they are not tamper-proof content hashes. Convert stopped, stable captures.
- Euler unwrap handles branch crossings, not gimbal lock. Quaternion output is available and changes the state feature width.
- The gripper channel of `observation.state` / `action` is the binary command (open=1, close=0), not a measured aperture. The measured aperture is exported separately as `observation.gripper_width` (see Joint angles); other hardware-specific semantics need their own input adapter rather than a reinterpretation of this channel.
- Validation is scoped to LEVI's own schema, row and media checks; it does not certify every training framework's loader.

## 验证范围 / Validation scope

Validation decodes every video (or reuses the conversion's own decode), checks frame counts against parquet rows, frame rate from packet timestamps, resolutions, `timestamp == frame_index / fps`, contiguous indices, task indices and metadata counts. It does not establish simulator or policy compatibility. Metadata repair accepts v2 only.
