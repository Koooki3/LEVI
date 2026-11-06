// Episode time -> what the 3D replay shows, into buffers allocated once.
//
// `at(t)` is called once per video frame while playing, so it allocates
// nothing of its own except inside `forwardPose`. The returned frame is the
// same object every time (mutated in place): read it, do not keep it.
//
// A value that is not a number (a hole in the data) leaves the previous
// value in place: the arm holds its pose over a gap instead of snapping to
// NaN or to zero.
import {
  FR3_JOINT_LIMITS,
  forwardPose,
} from "@/lib/robot-studio/fr3-kinematics";
import { indexAt, sampleAt, type SampleKind } from "./clock";
import type { ReplayData } from "./frames";
import { normalizedQuaternion, quaternionFromRpy, type Quat } from "./pose";

/** The trail is the last this many rows (the same cap as the URDF viewer's trail). */
export const MAX_TRAIL_POINTS = 300;

export interface ReplayFrame {
  /** Row at or before the time (state track). */
  index: number;
  /** Seven FR3 joints (joints mode only; zeros in pose mode). */
  joints: Float64Array;
  /** The action as seven joints (when `actionMode` is joints). */
  actionJoints: Float64Array;
  /**
   * The state's end effector: the flange from forward kinematics in joints
   * mode, the x, y, z columns in pose mode. Metres.
   */
  position: Float64Array;
  /** [x, y, z, w]; flange orientation (joints) or the pose columns (pose). */
  quaternion: Float64Array;
  /** The action as a pose (when `actionMode` is pose). */
  actionPosition: Float64Array;
  actionQuaternion: Float64Array;
  /** Gripper column value at the time (a step, never blended), or null. */
  gripper: number | null;
}

export interface ReplaySampler {
  frame: ReplayFrame;
  at(t: number): ReplayFrame;
  /** End-effector positions of every row, x, y, z in a row, computed on first use. */
  path(): Float32Array;
  /** Rows [from, to) of `path()` that make the trail ending at `index`. */
  trailRange(index: number): { from: number; to: number };
}

const finite = (values: ArrayLike<number>, count: number) => {
  for (let i = 0; i < count; i++) if (!Number.isFinite(values[i])) return false;
  return true;
};

/**
 * Interpolating between two values on a limit can land one ulp past it, and
 * `forwardPose` throws on a joint past its limit: clamp what was sampled.
 */
function clampToLimits(joints: Float64Array) {
  for (let c = 0; c < 7; c++) {
    const { lower, upper } = FR3_JOINT_LIMITS[c];
    joints[c] = Math.min(upper, Math.max(lower, joints[c]));
  }
}

const POSE_EULER: readonly SampleKind[] = [
  "linear",
  "linear",
  "linear",
  "angle",
  "angle",
  "angle",
];
const POSE_QUATERNION: readonly SampleKind[] = [
  "linear",
  "linear",
  "linear",
  "step",
  "step",
  "step",
  "step",
];

/** Position and quaternion of one sampled pose row into the two buffers; false when it has a hole. */
function readPose(
  row: Float64Array,
  rotation: "euler" | "quaternion" | null,
  position: Float64Array,
  quaternion: Float64Array,
): boolean {
  const width = rotation === "quaternion" ? 7 : 6;
  if (!finite(row, width)) return false;
  position[0] = row[0];
  position[1] = row[1];
  position[2] = row[2];
  const q: Quat =
    rotation === "quaternion"
      ? normalizedQuaternion(row[3], row[4], row[5], row[6])
      : quaternionFromRpy(row[3], row[4], row[5]);
  quaternion[0] = q[0];
  quaternion[1] = q[1];
  quaternion[2] = q[2];
  quaternion[3] = q[3];
  return true;
}

export function createReplaySampler(data: ReplayData): ReplaySampler {
  const frame: ReplayFrame = {
    index: 0,
    joints: new Float64Array(7),
    actionJoints: new Float64Array(7),
    position: new Float64Array(3),
    quaternion: Float64Array.of(0, 0, 0, 1),
    actionPosition: new Float64Array(3),
    actionQuaternion: Float64Array.of(0, 0, 0, 1),
    gripper: null,
  };
  const stateScratch = new Float64Array(data.state.width);
  const actionScratch = data.action
    ? new Float64Array(data.action.width)
    : null;
  const gripperScratch = new Float64Array(1);
  const stateKinds =
    data.mode === "pose"
      ? data.rotation === "quaternion"
        ? POSE_QUATERNION
        : POSE_EULER
      : "linear";
  const actionKinds =
    data.actionMode === "pose"
      ? data.actionRotation === "quaternion"
        ? POSE_QUATERNION
        : POSE_EULER
      : "linear";
  let path: Float32Array | null = null;

  /** FK of a joints row; false when the row has a hole. */
  const flange = (joints: ArrayLike<number>) => {
    if (!finite(joints, 7)) return null;
    return forwardPose(Array.from(joints));
  };

  const at = (t: number): ReplayFrame => {
    frame.index = indexAt(t, data.state.t);
    sampleAt(t, data.state, stateKinds, stateScratch);
    if (data.mode === "joints") {
      if (finite(stateScratch, 7)) {
        frame.joints.set(stateScratch);
        clampToLimits(frame.joints);
      }
      const pose = flange(frame.joints);
      if (pose) {
        frame.position.set(pose.position);
        frame.quaternion.set(pose.quaternion);
      }
    } else {
      readPose(stateScratch, data.rotation, frame.position, frame.quaternion);
    }
    if (data.action && actionScratch) {
      sampleAt(t, data.action, actionKinds, actionScratch);
      if (data.actionMode === "joints") {
        if (finite(actionScratch, 7)) {
          frame.actionJoints.set(actionScratch);
          clampToLimits(frame.actionJoints);
        }
      } else if (data.actionMode === "pose") {
        readPose(
          actionScratch,
          data.actionRotation,
          frame.actionPosition,
          frame.actionQuaternion,
        );
      }
    }
    if (data.gripper) {
      sampleAt(t, data.gripper, "step", gripperScratch);
      frame.gripper = Number.isFinite(gripperScratch[0])
        ? gripperScratch[0]
        : frame.gripper;
    }
    return frame;
  };

  const buildPath = (): Float32Array => {
    const out = new Float32Array(data.rows * 3);
    const joints = new Float64Array(7);
    let x = 0;
    let y = 0;
    let z = 0;
    for (let i = 0; i < data.rows; i++) {
      if (data.mode === "joints") {
        for (let c = 0; c < 7; c++) joints[c] = data.state.values[i * 7 + c];
        const pose = flange(joints);
        if (pose) [x, y, z] = pose.position;
      } else {
        const w = data.state.width;
        const row = data.state.values;
        if (finite([row[i * w], row[i * w + 1], row[i * w + 2]], 3)) {
          x = row[i * w];
          y = row[i * w + 1];
          z = row[i * w + 2];
        }
      }
      out[i * 3] = x;
      out[i * 3 + 1] = y;
      out[i * 3 + 2] = z;
    }
    return out;
  };

  return {
    frame,
    at,
    path: () => (path ??= buildPath()),
    trailRange: (index) => {
      const to = Math.max(0, Math.min(data.rows, index + 1));
      return { from: Math.max(0, to - MAX_TRAIL_POINTS), to };
    },
  };
}
