# 3D replay of an FR3 episode

[简体中文](REPLAY_3D.zh-CN.md)

The Episodes tab of the dataset viewer can show a 3D replay of the episode beside the video: the FR3 arm moving with the video, or, when the dataset has no joint angles, the path of the end effector and its coordinate axes. It follows the video, the playback bar and the chart cursor, so pause, seek and click-on-chart move all three together. Nothing is loaded from the network: the arm model is `public/robots/fr3/` (see [Robot Studio](ROBOT_STUDIO.md) for its source). The robot studio and this replay draw the arm from the same module, `src/components/robot-3d/fr3-model.tsx`.

SO-100/101, OpenArm and Unitree G1 datasets keep their own **3D Replay** tab (models from the upstream bucket); this panel does not replace it.

## When it appears

The viewer looks at `robot_type` in `meta/info.json` and at the column names of the episode (`<feature> | <name>`, from `features[...].names`):

| `robot_type` | Columns | Panel |
| --- | --- | --- |
| contains `fr3`, `franka` or `panda` | 7 joints named `joint_1..7`, `joint_0..6`, `fr3_joint1..7`, `panda_joint1..7`, or any of these with `_pos` (in `observation.joint_positions`, else `observation.state`, else another `observation.*`) | **Joint angles**: the arm is drawn. |
| contains `fr3`, `franka` or `panda`, or empty, `generic`, `generic_arm` | `x, y, z` and `rx, ry, rz` (or `qx, qy, qz, qw`) | **End-effector pose only**: path and axes, no arm. |
| empty, `generic`, `generic_arm` | seven joint columns | pose columns if there are any, otherwise nothing: the joints could belong to any 7-joint arm (xArm7 and Kinova Gen3 also say `joint_1..7`), so they are not drawn as an FR3 |
| anything else | | no panel |

The FR3 family is decided before the Unitree G1 name test, so `franka_g1x` is a Franka, never the G1 viewer. (`hasURDFSupport` itself still calls it a G1; that is recorded in `replay-kind.ts` and not changed here.)

Units are radians and metres. Joint values are clamped to the FR3 limits; a value more than 0.1 rad outside is reported on the panel ("are the values in radians?"). `rx, ry, rz` are roll, pitch and yaw as `levi/conversion/raw.py` writes them, unwrapped, applied about the fixed X, Y, Z axes in that order (extrinsic XYZ). This is three's `"ZYX"` order, **not** three's `Euler "XYZ"`; `src/lib/replay/pose.ts` converts and `pose.test.ts` pins it with known rotations. Quaternions are `[x, y, z, w]` and are held, not interpolated, between rows.

## What the panel shows

- The viewport (at least 280 px high; below the video on a narrow screen, beside it from 1200 px up). Drag to orbit, scroll to zoom; the **View** buttons (Perspective, Front, Top) do the same without a pointer.
- **Show action (target)**, when the dataset has an `action` of the same kind: a translucent second arm (joints) or faint axes (pose). Solid is `observation.state`; translucent is the commanded `action`, which is **not** a measurement. If the dataset's action is the next state (the default of LEVI's conversion), it leads the state by one frame only. The viewer cannot read the conversion's `action_semantics` (it is in `meta/levi_conversion.json`), so this is stated as a condition.
- **Trail**: the last 300 end-effector positions at most (the flange in joints mode).
- A text line for the current row, time and values (joint angles in degrees, or position and rotation), the gripper as a number and a bar, and notes (below). The viewport is a named group described by that text. The gripper is the **measured opening in metres** when the dataset has a column named `gripper_width` (for example `observation.gripper_width`; the bar is against the Robotiq 2F-85 stroke, and the value is blended between rows); otherwise it is the **command** in `observation.state | gripper` (open = 1, closed = 0, held until its row; never a measured width). A measured width wins over a command column wherever each is.
- The panel is open by default in both modes; hide it with the button in its header. The choice, the switches and the view are kept for the session.
- If the model or WebGL fails to load, an error state with a **Retry** button replaces the viewport; the video and charts are unaffected.

Space (play/pause) and ↑/↓ (previous/next episode) work as everywhere in the Episodes tab; pressed on the panel's own switches and view buttons they act on those controls only. The panel has no animation, and the camera views jump, so `prefers-reduced-motion` has nothing to turn off.

## How it stays in time with the video

Rows are found by their own `timestamp`, not by frame number divided by fps, so an episode the viewer has thinned still replays at the right time. For a segmented (v3) video the time is the video time minus the segment start, the same as the player and the chart cursor use.

While the first visible camera plays, the arm updates on every presented video frame (`requestVideoFrameCallback`, about 30 times a second). Paused, and on every seek, it takes the page's time (the playback bar, a chart click). A browser without `requestVideoFrameCallback` estimates between the player's `timeupdate` events (about four a second) from the clock and `playbackRate`, and never more than 0.5 s past the last one, so a stalled video cannot make the arm run ahead and jump back. The update path does not go through React rendering.

## Limits

- **Pose mode draws no arm.** Which frame the recorded pose is in (the flange `fr3_link8` or a gripper tip, and relative to which base) has not been verified, and a seven-joint arm has many joint solutions for one pose, so none is computed. The panel says "pose frame not verified".
- **A dataset that stores only the end-effector pose opens in pose mode**, and so do all the datasets LEVI converted before it could export joint angles. The arm is drawn for a dataset that has seven joint columns and an FR3-family `robot_type` (a DROID view, or a conversion that writes `observation.joint_positions` and sets `robot_type` to `franka_fr3`).
- **At most 4000 rows.** The viewer keeps at most 4000 rows per episode, evenly spaced; an episode with that many may be thinned, and motion between rows is interpolated linearly (angles are not blended across a ±π jump).
- **No gripper model.** The gripper is a value and a bar, whichever column it comes from. The Robotiq model of the robot studio is not used here.
- `observation.state` is drawn as recorded; the panel does not claim it is a calibrated measurement.
- The trail, the full path and the axes are drawn in the data's own frame, in metres.

## Where it lives

`src/lib/replay/` (pure code: `fr3-columns.ts` which columns, `replay-kind.ts` which view, `frames.ts` packing, `clock.ts` and `playback-clock.ts` time, `sampler.ts` frame at a time, `pose.ts` rotations) and `src/components/robot-3d/` (`fr3-model.tsx` shared arm, `replay-scene.tsx` the Canvas, `replay-rig.ts` trail and axes, `replay-panel.tsx` the panel). Tests are in the `__tests__` folders beside them. The panel and the scene are loaded lazily, so three.js is not in the viewer's main chunk.
