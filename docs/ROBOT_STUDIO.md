# FR3 Robot Studio

[简体中文](ROBOT_STUDIO.zh-CN.md)

Open **FR3 interactive preview** from the **FR3 3D** link of the top bar, from the card under the title of the marvel_workstation page, or in the command palette (`Ctrl+K` / `⌘K`), or visit `/robot-studio`; the page links back to marvel_workstation. This is a design workspace with optional read-only measured-state mapping. It loads pinned local FR3/tool models, lets you move and rotate a local flange target, and exports preview points. The optional state view reads existing passive snapshots; it does not send robot commands or record a physical demonstration.

## Using the preview

1. Wait for the local model to load. Select **Translate** or **Rotate** and drag an axis arrow or rotation ring. Drag the background to orbit and scroll to zoom. **View** hides the target handles.
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

The page contains no live transport, robot IP setting, robot command fetch or start-collection action. Software review and offline tests are separate from on-hardware acceptance.

## Robotiq 2F-85 preview

The preview includes a local articulated 2F-85 model from the ROS description already used on this host. Its drive joint and mimic joints retain their URDF geometry. The opening slider and hold-to-open/close buttons animate only this browser model; releasing, losing focus or hiding the page stops the animation. Opening is continuous: 1 means open and 0 means closed. The displayed width follows the model's nonlinear linkage geometry, not a linear knuckle-angle fraction. Model/source license and hashes are in [Robotiq SOURCE.md](../public/robots/robotiq-2f85/SOURCE.md).

The mount and TCP fields are **unverified drafts**. Both start unspecified. Until a mount is supplied, the tool is shown at the flange origin for illustration. Mount translation/quaternion is flange → gripper base; TCP is gripper base → proposed TCP. Editors use millimeters and XYZ Euler degrees; stored transforms use meters and xyzw. These values do not change Desk, tool calibration, load, the existing flange IK target or controller configuration. The optional TCP axes appear only when both transforms exist.

## Workspace and speed drafts

The boundary editor uses six coordinates in the robot base frame `fr3_link0`: minimum/maximum X, Y and Z. An applied draft draws an amber 3D box. Preview dragging, joint changes and saving are blocked for a requested flange target outside the box, while the last accepted arm pose remains visible. An unset box is explicitly unspecified, not an approval for unrestricted motion. The check concerns a flange **point**, not robot links, the tool swept volume, intervening paths or obstacles.

The configuration editor stores optional linear/angular speed, position/rotation step, command timeout and gripper rate. Empty values remain null; the page supplies no site-specific defaults. **These are exported drafts, not active hardware constraints.** Fast mouse movement does not transmit commands. A future execution supervisor must impose server-side limits independently of browser event rate: elapsed monotonic control time, translation and rotation rates, bounded increments, acceleration where required, current/target workspace and swept-tool checks, fresh measured feedback, sequence/expiry and a dead-man/watchdog. Existing controller limits, client enable, readiness and ownership rules cannot be overridden by these fields.

## Passive measured-state preparation

“Read snapshots” opts into `GET /api/levi/robot-studio/state`. It reads only an existing `LEVI_WORKSPACE/robot-studio/telemetry.json`; the page does not create a producer, subscribe to ROS, contact robot-server ports or start a controller. No producer is installed or started by this change. Missing data is therefore shown as “No snapshot producer connected”, not as a connected robot.

The reader accepts only a bounded regular file at that fixed path, rejects symbolic-link path components, and validates finite vectors, units, schema, timestamps and quaternions. States are fresh, stale, missing, invalid or unavailable. Only samples at most one second old expose a measurement. Future skew above 50 ms is invalid; smaller tolerated skew reports a nonnegative age. The browser also expires a response by monotonic elapsed time if polling stops. The authenticated route exposes no mutation/start/enable endpoint, and read success is not readiness or motion permission.

### Initial alignment and follow/edit modes

Selecting “Read snapshots” enters **Follow robot** mode automatically. Until the first fresh sample arrives, the arm/target are hidden and pose fields remain empty; the illustrative initial pose is never labelled measured. The first sample directly sets all seven model joints, the flange pose computed by FK and, when available, continuous gripper opening. Later samples update this read-only white model. No IK solve changes measured joints. A measured flange outside the draft box is displayed unchanged, with a notice; it cannot become an editable starting target while outside. Model mounting/TCP still use the unverified draft, so joint alignment does not establish measured tool calibration.

“Edit target from measured pose” rechecks freshness at click time and starts a local editable target from that feedback. Further feedback updates the blue measured overlay without overwriting the white target or its gripper draft, including during dragging. “Follow robot” resumes measured display; switching into follow cancels queued local dragging. Applied box edits never re-solve or alter the followed joint sample. A missing or expired sample freezes the last followed pose with an explicit lost/expired notice; it never restores illustrative initial joints. Stopping reading preserves the current local pose, and enabling reading again waits for a new fresh sample.

Measured gripper angle and measured physical width remain distinct source fields. Missing or model-out-of-range gripper feedback hides the tool in follow mode instead of inventing an opening; the raw feedback remains visible when present. In edit mode an unknown gripper does not replace the current local opening draft. Reported EE frame names are displayed verbatim rather than assumed to be the flange or Robotiq TCP.

The page requests the next snapshot 500 ms after the previous request completes, expires measurements by source age plus request/elapsed time, and aborts a request after 1.2 seconds. This is periodic read-only visualization, not a servo control loop. No live producer is installed by this change; it still requires a separately approved read-only integration before a real rig can populate these samples.

A separately reviewed read-only producer would publish this closed schema (example values below are synthetic, not this rig's calibration or feedback):

```json
{
  "schema": "levi.robot_studio.telemetry.v1",
  "robot": "fr3",
  "source_id": "read-only-producer",
  "sequence": 1,
  "sampled_at_epoch": 1800000000.0,
  "joints_rad": [0.0, -0.785398, 0.0, -2.356194, 0.0, 1.570796, 0.785398],
  "gripper": {"knuckle_angle_rad": 0.4},
  "reported_ee": null
}
```

All keys are required; gripper/reported_ee may be null. Gripper is exactly one of `knuckle_angle_rad` or `opening_width_m`. A reported EE object has `position_m`, `quaternion_xyzw`, `frame_id` and `end_frame`. A sample timestamp must describe source capture time, not file-write time. The current health summary lacks these measured joint/gripper fields and is not a substitute. A synthetic fixture must never be presented as hardware telemetry.

## Extended draft export

Tool-aware export uses `levi_robot_preview_v2`. Each pose snapshots its continuous opening and unverified mount/TCP. The execution draft carries units in its field names and, when present, a base-frame workspace box with `checks_tool_sweep: false`. Existing poses are rechecked against the current box and marked true/false/null; they are not silently converted into approved commands. `hardware_execution`, `calibrated_tool`, `collision_checked` and `execution_draft.motion_enabled` stay false. The older helper format remains available for flange-only drafts without tool/config data.

Live movement, a live snapshot producer, real tool calibration and collection remain separate integration tasks.


## Supervised translation software (offline acceptance)

The manual-session panel prepares a relative **translation-only** drag session.
It reuses the preview workspace box, linear speed and position step draft. All
additional time, displacement and feedback tolerances start blank; the page does
not infer site-approved physical limits. The extra fields are software test
configuration, not calibrated robot operating limits. Rotations and gripper
commands are excluded from this execution session; gripper opening remains a
continuous local model preview.

The product currently reports `stop_unproven` and **cannot start this session**.
Its real transport factory always refuses, regardless of the evaluation client's
`enabled` switch or form parameters. This version adds no robot connection,
controller startup, error clearing, homing or physical gripper command.
The synthetic transport is injected by Python tests only, never selected by a
browser request, environment switch or public fake/real toggle.

With an injected test backend, a person reviews a one-time challenge, types its
phrase and checks the three attestations. The backend creates an authoritative
anchor (reading identity, seven joints, position and exact quaternion); the model
aligns from those joints. Dragging translates relative to that anchor in base
coordinates and preserves its orientation. The server checks reading/tool/path
conditions, the complete displacement, each bounded step and Euclidean speed,
workspace, strictly increasing sequences and deadlines. It coalesces targets and
allows one in-flight send. Idle time cannot accumulate a larger movement budget.
A preview plan hash or a browser-provided current pose never grants execution.

Release, cancellation, Escape, loss of focus, a hidden/unmounted page, invalid
feedback or expired target/deadman closes admission and discards pending goals.
An unknown or late acknowledgment locks the session against resumption. Closing
admission proves only that this sender admits no further command: it is not a
physical stop. Counters distinguish accepted targets, local sends,
acknowledgments and synthetic settled evidence. The existing `run.Supervisor`
and arm lock own this mode too, so hosted evaluation/stack/service actions keep
their normal active-job protections. The lock remains held when stopping or
command delivery is unproven; the HTTP end body cannot supply stop evidence.
Both challenge and start reject active or unreadable AERI ownership, including a hosted terminal D that has not yet taken the arm lock.

A future real transport requires a separately reviewed stop actuator/observer,
site limits and tool/base calibration, trusted live feedback/planner integration,
and explicit hardware commissioning approval. The pure injected reader/path
boundary avoids duplicating the parallel MARVEL0 state/IK/planner work. Software
fixtures establish no physical stopping distance, collision clearance or safe
velocity. No motion automatically resumes after a restart.

A start response can be lost after acceptance. State exposes the originating `start_request_id`; the page retains its own request ID and uses one bounded GET to recover only that synthetic owner for stop/end. It never arms from recovery. An unmatched/unknown request stays `stop_unproven`; explicit Refresh or Stop may check it again, without a background retry loop. Stop/end responses must match the owner and explicitly report closed admission.

## Parameter capabilities and proposals

The parameter panel separates local preview drafts, Cartesian impedance updates awaiting per-parameter acceptance/readback, gripper startup configuration, and unsupported online joint-torque/acceleration/force-threshold changes. This is a reviewed software capability list, not a probe of the live controller. Current controller values remain unavailable. No parameter mutation endpoint or real transport is added.

The optional limit proposal starts empty. Joint torque and acceleration use complete seven-joint groups; Cartesian acceleration and external-force stop thresholds are scalar proposals. A partially filled group or a nonpositive/nonfinite value blocks export. No hardware maximum or operating default is inferred. These names explicitly denote limits/stop thresholds, not torque or contact-force control targets. The server's Cartesian torque observation must not be presented as seven joint torques.

“Export proposal” downloads `levi.robot_studio.parameter_proposal.v1` with units in the keys, unknown groups as null, and `hardware_execution`, `hardware_values_read` and `approved_limits` all false. Every capability has `can_apply: false`. It is separate from the pose preview and does not change the manual-session configuration, its deadman, or its stop gate. Controller updates need validated ranges, authoritative acceptance/readback and a separately reviewed adapter; gripper force/speed changes require a reviewed startup-configuration workflow.

Manual-session counters appear as a compact four-cell grid. Accepted targets, local sends, acknowledgments and synthetic settled evidence retain their separate meanings and display unknown when absent.
