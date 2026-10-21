# FR3 Robot Studio

[简体中文](ROBOT_STUDIO.zh-CN.md)

Open **FR3 interactive preview** from the **More** menu of the top bar, from the card under the title of the MARVEL0 page, or in the command palette (`Ctrl+K` / `⌘K`), or visit `/robot-studio`; the page links back to MARVEL0. This is a browser-only design workspace. It loads a pinned, local FR3 model, lets you move and rotate the flange target, and exports preview points. It does not connect to a robot or record a physical demonstration.

## Using the preview

1. Wait for the local model to load. Select **Translate** or **Rotate** and drag an axis arrow or rotation ring. Drag the background to orbit and scroll to zoom. **View** hides the target handles.
2. Choose base or flange axes for the handles. Numeric position fields always use the base frame in millimeters; orientation fields use XYZ Euler angles in degrees. Internally the solver uses meters and `xyzw` quaternions.
3. Edit fields and choose **Apply target** for keyboard operation. Expand **Joints and limits** to explore individual joint angles. **Esc** during a drag restores the pose at its start; losing window focus also cancels a drag. **Restore initial preview** restores an illustrative model pose only.
4. An unsolved target remains visible while the model keeps its last valid pose. The page reports the solver residual and prevents saving that target. A numerical solver can fail even for a reachable target near limits; it is not a reachability proof.
5. **Add pose** saves a solved configuration in memory (up to 100). **Undo last** removes the last saved point; **Export JSON** downloads the draft. Reloading or leaving the page discards unexported points. No timestamps, speed profile or interpolation are implied.

The page follows LEVI's theme and language. The media viewport stays dark; the inspector follows light/dark preference. Inputs remain available without pointer dragging. A WebGL/model failure has a retry action; there is no substitute robot model.

## Model and computation

The rendering and solver share the official FR3 nominal chain, base `fr3_link0` to flange `fr3_link8`. The model ends at the flange: it does not claim the dimensions or TCP calibration of a mounted Robotiq or other tool. The illustrative initial joints are not measured state or a home command. The bundled assets include their fixed upstream revision, source/output hashes, conversion description and Apache-2.0 notice in [SOURCE.md](../public/robots/fr3/SOURCE.md) and [SOURCE.json](../public/robots/fr3/SOURCE.json).

Forward kinematics is checked against the URDF transform chain. Inverse kinematics uses bounded, seeded damped least squares and joint limits; an unsuccessful solve never applies a partial result. Drawing is demand-driven. Meshes load locally, without a third-party environment image or external runtime CDN.

Joint limits and an IK solution do **not** establish collision freedom, dynamics, calibration or physical safety. Self-collision and scene collision checking are not implemented.

## Draft export and future collection

`levi_robot_preview_v1` identifies the robot, base/end frames and units, with `hardware_execution: false`, `collision_checked: false`, and `calibrated_tool: false`. Each point contains seven joint angles and the flange pose recomputed from them. It is a design artifact, not a dataset episode or a ready-to-execute robot trajectory. No real-robot endpoint accepts this file in this change.

A later integration belongs behind a separately reviewed backend adapter, not in the Canvas mouse handler:

- Bind an approved robot profile, measured initial joint state, camera sources, and calibrated tool transform. Do not infer missing calibration or pretend the flange is the gripper TCP.
- Own a single control lease across web input, SpaceMouse and policy execution. Validate command freshness, reference frames and limits; cancel the target on release, lost focus or connection expiry. A frontend message alone cannot establish that the robot stopped.
- Route bounded targets through the existing robot controller. Check actual measured feedback, collision constraints and faults on the backend; never let a stale browser target keep streaming indefinitely.
- Record camera observations, measured state, the post-limit action actually sent, gripper conventions, acquisition timestamps and control source. Keep the proposed preview target distinct from the command and the measured state.
- Preserve the existing human-only authorization for robot motion and collection, and use the coordinated service/driver ownership protocol.

The page contains no live transport, robot IP setting, robot command fetch or start-collection action. Software review and offline tests are separate from on-hardware acceptance.
