# 内置转换 / Built-in conversion

LEVI includes the complete capture workflow. All stages run in its uv environment through `uv run levi convert`; no external scripts or training framework are required.

## 输入 / Capture schema

```text
captures/session-a/
└── task-a/
    ├── task_description.txt
    └── demo_001/
        ├── end_effector_pose.csv
        ├── gripper_state.csv
        ├── wrist_camera.mp4
        └── side_camera.mp4
```

- Pose columns: `timestamp_sec, frame_index, success_flag, source_stamp_sec, px, py, pz, qx, qy, qz, qw`.
- Gripper columns: `timestamp_sec, frame_index, success_flag, source_stamp_sec, finger_left, finger_right, gripper_width, last_gripper_command`.
- Quaternion order: XYZW. Position: metres. Timestamps: seconds. Commands: `open` / `close` only. Successful source samples have `success_flag=1`.
- Each CSV row corresponds to the same ordered video/image frame. Frame IDs must be unique, integer and increasing; pose/gripper IDs and capture timestamps must agree. Headerless CSV is accepted only when its numeric leading row and column count match the documented schema.
- Image input uses `wrist_camera/` and `side_camera/` directories with naturally sorted PNG/JPEG files. Set `source_fps` to their actual capture rate. AVI fallback is named `<camera>_raw.avi`.
- Additional CSVs with `frame_index` must use the same row IDs. Events with `frame_index` map to the next retained frame during filtering; original frame/time fields are preserved. Unindexed event rows retain their original timing semantics.
- Task text comes from `task_description.txt`, falling back to the task directory name. No project-specific language corrections are bundled.

必须先确认输入本来已同步。设置 FPS 只改变采样或规范输出时间轴，不能修复相机与机器人状态的未知错位。缺失帧、重复帧编号、未知夹爪指令不会被静默忽略。输出不会覆盖已有目录。

## 阶段 / Stages

| Stage | Input → output | Behavior |
| --- | --- | --- |
| `pipeline` | raw → run directory with `dataset/` | Independent staging, image/FPS normalization, preflight, optional static filtering, v2.1 conversion, full validation |
| `summary` | raw → report | Counts, cameras, FPS, quality findings, expected retained frames |
| `images` | image capture → new raw copy | Resamples images and every aligned CSV together; H.264/yuv420p output |
| `fps-preview` | raw → report | Image count/FPS or video alignment checks |
| `fps` | raw → new raw copy | Rebuilds images/video and CSV at target FPS; never deletes stale source videos |
| `frozen` | raw → report | Full sequential camera scan; deviation from first frame; shared first-frame hashes |
| `stage-preview` | raw → report | Structural, numeric, capture completion (optional), stale state, count/FPS and frozen-camera checks |
| `stage` | raw → independent snapshot | Quality-gated copy; recovers valid raw AVI into H.264 if needed |
| `static` / `filter-preview` | raw → report | Preview retained frame counts with configurable geometry/gripper rules |
| `filter` | raw → new raw copy | Identical retained positions for CSVs and cameras; gripper transitions and end frames protected |
| `convert` | raw → LeRobot v2.1 | Strict preflight, actual video metadata/statistics, canonical timestamps and provenance |
| `timestamps` | v2 → new v2 copy | Rebuilds timestamp column; preserves old values in metadata; refreshes available timestamp statistics; refuses FPS inconsistent with video |
| `tasks-preview` | v2 → report | Explicit task map preview; unresolved slug identifiers fail |
| `tasks` | v2 → new v2 copy | Applies supplied map to tasks and episode metadata; task IDs unchanged |
| `validate` | v2/v3 → report | Native diagnostics plus full video decode; additional v2 row/index/task/media contracts |

Read-only stages print structured JSON and create no dataset. Web jobs persist their JSON result beside the logs. `pipeline` leaves `capture/`, optional `filtered/`, `dataset/`, `preflight.json` and `validation.json` in its new run directory. Intermediate datasets are durable outputs, not disposable caches.

所有输入先做符号链接检查；计划保存源文件大小/修改时间指纹，执行前后检查采集是否变化。建议在停止录制后处理；指纹不是用于对抗恶意修改的内容哈希。服务重启不自动续写任务，部分输出保留并标注中断。

## 配置 / Options

Save JSON inside the workspace, e.g. `configs/conversion.json`:

```json
{
  "fps": 10,
  "source_fps": 30,
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
  "filter_static": true,
  "orientation": "euler",
  "action_mode": "next_state",
  "robot_type": "generic_arm",
  "task_map": {"pick_object": "Pick up the object."},
  "exclude_demos": ["task-a/demo_004"]
}
```

Remove the example exclusion if that path does not exist; unknown paths are rejected. In the web UI, the separate FPS fields take precedence over FPS values in advanced JSON. The same rule applies to explicit CLI `--fps` / `--source-fps` flags.

```bash
uv run levi convert stage-preview --source captures/session-a \
  --output datasets/unused-preview --options configs/conversion.json
uv run levi convert pipeline --source captures/session-a \
  --output datasets/run-a --options configs/conversion.json
uv run levi convert validate --source datasets/run-a/dataset \
  --output datasets/unused-validation
```

- `xyz_threshold`: retained-frame displacement in metres; default 5 mm.
- `rotation_threshold`: quaternion geodesic distance in radians; default 0.01 rad. `q` and `-q` are the same rotation.
- `gripper_margin`: protect ±N source frames around command transitions.
- `frozen_threshold`: maximum mean absolute difference from the first 32×32 thumbnail, on the 0–255 scale. Values below the threshold fail preflight; 0 disables this heuristic. Identical first frames across demos are reported separately and do not alone prove corruption.
- `stale_run`: maximum tolerated consecutive zero deltas in source robot timestamps before a quality failure.
- `require_complete`: additionally require capture metadata stop markers and `stop_demo` events. Constant gripper command is reported as a warning, not automatically invalid for every task.
- `orientation`: `euler` uses normalized quaternions → XYZ Euler → per-axis unwrap; Euler singularities remain possible, so choose `quaternion` for data crossing them. Quaternion mode enforces sign continuity and changes state width from 7 to 8.
- `action_mode`: `next_state` (default) or `state`. No delta-action or measured-command semantics are inferred.

## 改进与语义 / Algorithms and semantics

The static filter compares each sample with the **last retained frame**, avoiding deletion of an entire slow trajectory whose adjacent displacements are tiny. It preserves the first/last frame and gripper transition margins. Filtering compresses elapsed time: timestamps become retained `frame_index / fps`, with original times and IDs saved for traceability. It is an explicit dataset transformation, not a physical velocity-preserving resample.

Image/FPS normalization selects ordered samples at `source_fps / target_fps`; upsampling is rejected. Camera frame count, order and rate must agree before resampling. Output geometry must be even-sized for yuv420p. Videos are sequentially decoded and streamed to FFmpeg using bounded frame memory; no whole-video frame array is stored.

Conversion writes float32 action/state vectors and timestamp, int64 indices, truthful camera shape/codec/FPS and measured RGB statistics over **all decoded output pixels**. Per-episode and aggregate statistics use weighted population moments. H.264 encoding is lossy (CRF 18); original footage remains unchanged.

Default state is `[x,y,z,rx,ry,rz,gripper_command]`; next-state action is `[state[t+1]]`, repeating the final state. Gripper command is open=1 / close=0 and does not describe measured jaw width. The collector's hardware-specific knuckle-angle helper is not required for this representation and is not imported.

## 审核交接 / Review handoff

`meta/levi_provenance.jsonl` records `episode_index`, relative `source_demo`, original `source_frame_ids` and source capture timestamps. `meta/levi_conversion.json` records options and units. Use these to map flagged episodes to capture paths, review them, then configure `exclude_demos`; never assume episode N equals `demo_N`.

Task corrections are user-supplied mappings. Preview lists edits and unknown slug-like instructions; applying unknown mappings fails before creating an output. Chinese and other language task contents are preserved unless explicitly mapped.

Exit codes: 0 = stage accepted; 2 = completed report contains failing checks; 1 = execution/configuration/preflight error. Web job status uses both the worker exit code and its structured `ok` result. No errors are converted into success by skipping bad demos. Explicit exclusions are included in the report.

## 验证范围 / Validation scope

Conversion output is v2.1, while browsing and native validation also accept v3. Full video decode does not establish simulator or policy compatibility. Native v3 validation is not a complete schema validator for every shared-shard edge case. Metadata repair deliberately accepts v2 only. Video codecs supported by FFmpeg/OpenCV and camera layout must be compatible with the input adapter.

Reference format and encoding documentation: [LeRobot format migration](https://github.com/huggingface/lerobot/blob/main/docs/source/porting_datasets_v3.mdx), [FFmpeg rawvideo](https://www.ffmpeg.org/ffmpeg-formats.html#rawvideo), [FFmpeg encoding options](https://ffmpeg.org/ffmpeg.html).
