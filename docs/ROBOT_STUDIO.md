# FR3 Robot Studio

[简体中文](ROBOT_STUDIO.zh-CN.md)

Open **FR3 interactive preview** from the **FR3 3D** link of the top bar, from the card under the title of the marvel_workstation page, or in the command palette (`Ctrl+K` / `⌘K`), or visit `/robot-studio`; the page links back to marvel_workstation. This is a design workspace with optional read-only measured-state mapping. It loads pinned local FR3/tool models, lets you move and rotate a local flange target, and exports preview points. The default state view follows read-only measured feedback. A separately confirmed manual session can send bounded translations and finite gripper goals; opening the page starts no controller or motion.

## Using the preview

1. Wait for fresh measured feedback and the local model. In the viewport toolbar choose **Edit target**, then **Translate** or **Rotate** and drag an axis arrow or rotation ring. **Follow measured state** returns to the measured view. Drag the background to orbit and scroll to zoom. **View** hides the target handles.
2. Choose base or flange axes for the handles. Numeric position fields always use the base frame in millimeters; orientation fields use XYZ Euler angles in degrees. Internally the solver uses meters and `xyzw` quaternions.
3. Edit fields and choose **Apply target** for keyboard operation. Expand **Joints and limits** to explore individual joint angles. **Esc** during a drag restores the pose at its start; losing window focus also cancels a drag. **Restore initial preview** restores an illustrative model pose only.
4. An unsolved target remains visible while the model keeps its last valid pose. The page reports the solver residual and prevents saving that target. A numerical solver can fail even for a reachable target near limits; it is not a reachability proof.
5. **Add pose** saves a solved configuration in memory (up to 100). **Undo last** removes the last saved point; **Export JSON** downloads the draft. Reloading or leaving the page discards unexported points. No timestamps, speed profile or interpolation are implied.

The page follows LEVI's theme and language. The media viewport stays dark; the inspector follows light/dark preference. Inputs remain available without pointer dragging. A WebGL/model failure has a retry action; there is no substitute robot model.

## Model and computation

The rendering and solver share the official FR3 nominal chain, base `fr3_link0` to flange `fr3_link8`. The arm chain ends at the flange; a separate nominal Robotiq model illustrates the tool without claiming measured installation or TCP calibration. The illustrative initial joints are not measured state or a home command. The bundled assets include their fixed upstream revision, source/output hashes, conversion description and Apache-2.0 notice in [SOURCE.md](../public/robots/fr3/SOURCE.md) and [SOURCE.json](../public/robots/fr3/SOURCE.json).

Forward kinematics is checked against the URDF transform chain. Inverse kinematics uses bounded, seeded damped least squares and joint limits; an unsuccessful solve never applies a partial result. Drawing is demand-driven. Meshes load locally, without a third-party environment image or external runtime CDN.

Joint limits and an IK solution do **not** establish collision freedom, dynamics, calibration or physical safety. Self-collision and scene collision checking are not implemented.

## Draft export and future collection

Tool-aware `levi_robot_preview_v2` (or flange-only helper `levi_robot_preview_v1`) identifies the robot, base/end frames and units, with `hardware_execution: false`, `collision_checked: false`, and `calibrated_tool: false`. Each point contains seven joint angles and the flange pose recomputed from them. It is a design artifact, not a dataset episode or a ready-to-execute robot trajectory. No real-robot endpoint accepts this file in this change.

A later integration belongs behind a separately reviewed backend adapter, not in the Canvas mouse handler:

- Bind an approved robot profile, measured initial joint state, camera sources, and calibrated tool transform. Do not infer missing calibration or pretend the flange is the gripper TCP.
- Own a single control lease across web input, SpaceMouse and policy execution. Validate command freshness, reference frames and limits; cancel the target on release, lost focus or connection expiry. A frontend message alone cannot establish that the robot stopped.
- Route bounded targets through the existing robot controller. Check actual measured feedback, collision constraints and faults on the backend; never let a stale browser target keep streaming indefinitely.
- Record camera observations, measured state, the post-limit action actually sent, gripper conventions, acquisition timestamps and control source. Keep the proposed preview target distinct from the command and the measured state.
- Preserve the existing human-only authorization for robot motion and collection, and use the coordinated service/driver ownership protocol.

Preview editing and export send no motion. Explicit manual translation and the separate real-gripper controls below use guarded backend transports. Software review and offline tests are separate from on-hardware acceptance.

## Robotiq 2F-85 preview

The preview includes a local articulated 2F-85 model from the ROS description already used on this host. Its drive joint and mimic joints retain their URDF geometry. The opening slider and hold-to-open/close buttons animate only this browser model; releasing, losing focus or hiding the page stops the animation. Opening is continuous: 1 means open and 0 means closed. The displayed width follows the model's nonlinear linkage geometry, not a linear knuckle-angle fraction. Model/source license and hashes are in [Robotiq SOURCE.md](../public/robots/robotiq-2f85/SOURCE.md).

The mount and TCP fields are **unverified drafts**. Both start unspecified. Until a mount is supplied, the tool is shown at the flange origin for illustration. Mount translation/quaternion is flange → gripper base; TCP is gripper base → proposed TCP. Editors use millimeters and XYZ Euler degrees; stored transforms use meters and xyzw. These values do not change Desk, tool calibration, load, the existing flange IK target or controller configuration. The optional TCP axes appear only when both transforms exist.

## Workspace and speed drafts

The boundary editor uses six coordinates in the robot base frame `fr3_link0`: minimum/maximum X, Y and Z. An applied draft draws an amber 3D box. Preview dragging, joint changes and saving are blocked for a requested flange target outside the box, while the last accepted arm pose remains visible. An unset box is explicitly unspecified, not an approval for unrestricted motion. The check concerns a flange **point**, not robot links, the tool swept volume, intervening paths or obstacles.

The configuration editor stores optional linear/angular speed, position/rotation step, command timeout and gripper rate. Empty values remain null; the page supplies no site-specific defaults. **These are exported drafts, not active hardware constraints.** Fast mouse movement does not transmit commands. A future execution supervisor must impose server-side limits independently of browser event rate: elapsed monotonic control time, translation and rotation rates, bounded increments, acceleration where required, current/target workspace and swept-tool checks, fresh measured feedback, sequence/expiry and a dead-man/watchdog. Existing controller limits, client enable, readiness and ownership rules cannot be overridden by these fields.

## Read-only measured-state integration

The page opens in **Follow robot** mode with read-only polling enabled. On the real product, an explicit `[manual] enabled = true` in the local control file selects the reviewed Robot Server v1 reader at the site's fixed `server.robot_server_url`. It performs only `GET /manual/v1/state`; it does not start a manual session, activate a controller, or send a pose/gripper command. The server must have the reviewed integration in [`integrations/fr3_manual`](../integrations/fr3_manual/README.md) installed and restarted by the operator. A missing/old/unreachable source is shown as unavailable, with no illustrative pose presented as measured.

When real-source configuration is off, the existing bounded `LEVI_WORKSPACE/robot-studio/telemetry.json` reader remains available. Live reads use source monotonic timestamps and independent arm/gripper freshness; an unavailable gripper does not erase valid arm joints. The server reports its Desk EE separately: it is not presumed to equal model `fr3_link8`.
The reader accepts only a bounded regular file at that fixed path, rejects symbolic-link path components, and validates finite vectors, units, schema, timestamps and quaternions. States are fresh, stale, missing, invalid or unavailable. Only samples at most one second old expose a measurement. Future skew above 50 ms is invalid; smaller tolerated skew reports a nonnegative age. The browser also expires a response by monotonic elapsed time if polling stops. The authenticated route exposes no mutation/start/enable endpoint, and read success is not readiness or motion permission.

### Initial alignment and follow/edit modes

Opening the page or reconnecting enters **Follow robot** mode automatically. Until the first fresh sample arrives, the arm/target are hidden and pose fields remain empty; the illustrative initial pose is never labelled measured. The first sample directly sets all seven model joints, the flange pose computed by FK and, when available, continuous gripper opening. Later samples update this read-only white model. No IK solve changes measured joints. A measured flange outside the draft box is displayed unchanged, with a notice; it cannot become an editable starting target while outside. Model mounting/TCP still use the unverified draft, so joint alignment does not establish measured tool calibration.

“Edit target from measured pose” rechecks freshness at click time and starts a local editable target from that feedback. Further feedback updates the blue measured overlay without overwriting the white target or its gripper draft, including during dragging. “Follow robot” resumes measured display; switching into follow cancels queued local dragging. Applied box edits never re-solve or alter the followed joint sample. A missing or expired sample freezes the last followed pose with an explicit lost/expired notice; it never restores illustrative initial joints. Stopping reading preserves the current local pose, and enabling reading again waits for a new fresh sample.

Measured gripper angle and measured physical width remain distinct source fields. Missing or model-out-of-range gripper feedback hides the tool in follow mode instead of inventing an opening; the raw feedback remains visible when present. In edit mode an unknown gripper does not replace the current local opening draft. Reported EE frame names are displayed verbatim rather than assumed to be the flange or Robotiq TCP.

The page requests the next snapshot 500 ms after the previous request completes, expires measurements by source age plus request/elapsed time, and aborts a request after 1.2 seconds. This is periodic read-only visualization, not a servo control loop. The reviewed Robot Server integration supplies these measurements after its operator-controlled restart. The rendering remains periodic visualization, not motion control.

An optional file-based read-only producer uses this closed schema (example values below are synthetic, not this rig's calibration or feedback):

```json
{
  "schema": "levi.robot_studio.telemetry.v1",
  "robot": "fr3",
  "source_id": "read-only-producer",
  "sequence": 1,
  "sampled_at_epoch": 1800000000.0,
  "joints_rad": [0.0, -0.785398, 0.0, -2.356194, 0.0, 1.570796, 0.785398],
  "gripper": { "knuckle_angle_rad": 0.4 },
  "reported_ee": null
}
```

All keys are required; gripper/reported_ee may be null. Gripper is exactly one of `knuckle_angle_rad` or `opening_width_m`. A reported EE object has `position_m`, `quaternion_xyzw`, `frame_id` and `end_frame`. A sample timestamp must describe source capture time, not file-write time. The current health summary lacks these measured joint/gripper fields and is not a substitute. A synthetic fixture must never be presented as hardware telemetry.

## Extended draft export

Tool-aware export uses `levi_robot_preview_v2`. Each pose snapshots its continuous opening and unverified mount/TCP. The execution draft carries units in its field names and, when present, a base-frame workspace box with `checks_tool_sweep: false`. Existing poses are rechecked against the current box and marked true/false/null; they are not silently converted into approved commands. `hardware_execution`, `calibrated_tool`, `collision_checked` and `execution_draft.motion_enabled` stay false. The older helper format remains available for flange-only drafts without tool/config data.

Tool calibration and demonstration collection remain separate from this display and manual-control session.

## Supervised manual translation and gripper control

The real adapter is opt-in through `[manual] enabled = true`. The default test host remains disabled, and synthetic factories exist only for offline tests. The UI requires a one-time challenge, the phrase `start real translation session`, and explicit operator attestations. No browser pose, preview hash or telemetry read grants motion permission.

The session starts from fresh server feedback and owns the same arm lock as hosted evaluation. Dragging applies base-frame translation relative to that measured anchor; orientation stays fixed. The visual flange comes from measured joints and FK, while the backend applies the same translation delta to Desk EE. Rotation remains local preview only. Workspace, displacement, per-step and speed limits, feedback age, anchor age, heartbeat and target expiry are explicit required values; they have no inferred physical defaults. Four additional explicit stop thresholds are `settle_window_s`, `settle_position_m`, `settle_angle_rad`, and `gripper_settle_rad`.

The viewport inspector starts with **Real gripper control**, independent of the translation session. Click Open/Close once, or adjust the target slider and release it to send one finite goal through stack B. The click/release acknowledges the inline hands-clear notice. The goal continues after release until its result or the existing timeout/cancellation; pointer cancellation, Escape or blur before release sends nothing. Fresh measured gripper feedback, B readiness, a free arm lock, no hosted run or client and no other operation remain required. Network uncertainty disables further sends without automatic retry. Measured opening and target opening are displayed separately. The older **Robotiq 2F-85 preview** slider still changes only the local model.

`POST /api/levi/marvel0/stack/b/gripper` retains its human-only guard. Existing open/close bodies are unchanged. A position request uses `action: "position"`, `opening_ratio` in `[0,1]`, `confirm: "gripper position"` and `attest: {"hands_clear": true}` plus a unique `request_id`. The ratio is nominal jaw-opening width (0 closed, 1 open), converted with the same nonlinear geometry as the model; no mounting calibration is inferred. The same finite ROS action, exclusivity, result checks, timeout and cancellation apply.

The v1 server independently enforces identity, sequence, expiry and bounds, serializes legacy mutations against manual ownership, and watches freshness without browser participation. Closing sends a measured hold pose and cancels the exact gripper action, including a goal accepted late. Neither HTTP acknowledgement nor cancellation acknowledgement is physical stop evidence: release of ownership requires subsequent fresh arm/gripper feedback within the configured settle window plus a terminal action result. Unknown delivery or stop retains ownership. The end body cannot provide this evidence.

A failed capabilities read before start leaves the local lock available. Once a start may have reached the server, the owner is retained until proved ended. State includes `start_request_id` for bounded recovery of this page's own request; recovery never rearms motion. Stop proof is explicitly physical or synthetic. A successful remote end receipt can be reused locally after an audit failure without sending end again.

Installing the integration does not start the Robot Server. Its existing startup may activate impedance, so the operator performs that restart and hardware acceptance. Offline tests establish protocol behavior only, not stopping distance, collision clearance, tool calibration or safe operating limits. No automatic motion resumes after a restart.

## Parameter capabilities and proposals

The parameter panel separates local preview drafts, Cartesian impedance updates awaiting per-parameter acceptance/readback, gripper startup configuration, and unsupported online joint-torque/acceleration/force-threshold changes. This is a reviewed software capability list, not a probe of the live controller. Current controller values remain unavailable. No parameter mutation endpoint is added; manual motion transport does not implement online torque, acceleration, impedance or force-configuration writes.

The optional limit proposal starts empty. Joint torque and acceleration use complete seven-joint groups; Cartesian acceleration and external-force stop thresholds are scalar proposals. A partially filled group or a nonpositive/nonfinite value blocks export. No hardware maximum or operating default is inferred. These names explicitly denote limits/stop thresholds, not torque or contact-force control targets. The server's Cartesian torque observation must not be presented as seven joint torques.

“Export proposal” downloads `levi.robot_studio.parameter_proposal.v1` with units in the keys, unknown groups as null, and `hardware_execution`, `hardware_values_read` and `approved_limits` all false. Every capability has `can_apply: false`. It is separate from the pose preview and does not change the manual-session configuration, its deadman, or its stop gate. Controller updates need validated ranges, authoritative acceptance/readback and a separately reviewed adapter; gripper force/speed changes require a reviewed startup-configuration workflow.

Manual-session counters appear as a compact four-cell grid. Accepted targets, local sends, acknowledgments and synthetic settled evidence retain their separate meanings and display unknown when absent.

## Translation configuration workflow

The viewport toolbar exposes follow/edit directly. The translation card lists missing configuration with links that open and focus the relevant input. Speed/step drafts remain accessible while following feedback. Workspace limits and all session/stop thresholds stay empty until explicitly provided. An out-of-bound measured flange stays visible and points to workspace configuration instead of silently enabling editing.

**Import / export manual configuration** uses `levi.robot-studio.manual-config.v1` JSON with complete `limits` and `stop_limits`. Paste previously reviewed numeric settings and choose Import into draft; exporting downloads the complete configuration. Import never starts a session, restores attestations or supplies safe defaults. The operator still reviews the draft and explicitly starts each real translation session. Unsupported rotation, physical calibration and online dynamics writes remain unchanged.
