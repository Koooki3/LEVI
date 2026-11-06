// `flatChartData` rows -> the typed arrays the 3D replay reads, once at load
// (design: lab/levi-hub/programs/levi2/REPLAY-3D-DESIGN-20261021.md §3.1, §3.4).
//
// The detection (`detectFr3Replay`) says which keys are which; this packs the
// values and checks them. Pure: no DOM, no three.
import { FR3_JOINT_LIMITS } from "@/lib/robot-studio/fr3-kinematics";
import { packTrack, type ReplayTrack } from "./clock";
import type {
  Fr3ReplayDetection,
  Fr3ReplayMode,
  Fr3Rotation,
} from "./fr3-columns";

/** A joint this far outside its limit (rad) is not an FR3 joint angle; closer is clamped silently. */
export const JOINT_LIMIT_TOLERANCE = 0.1;
/**
 * The viewer keeps at most this many rows per episode (`MAX_EPISODE_POINTS`
 * in `fetch-data.ts`, 4000 by default), evenly sampled. A track this long may
 * have been thinned, so the panel says so.
 */
export const SAMPLED_ROW_COUNT = 4000;

export type ReplayWarning =
  /** `rows` rows had a joint beyond its limit and were clamped; `farRows` of them by more than the tolerance. */
  | {
      kind: "joints-clamped";
      track: "state" | "action";
      rows: number;
      farRows: number;
    }
  /** Rows whose state values are not numbers (held at the previous row's pose on screen). */
  | { kind: "missing-values"; rows: number };

export interface ReplayData {
  mode: Exclude<Fr3ReplayMode, "none">;
  /** Joints 1..7 (width 7), or x, y, z + rx, ry, rz (6) / qx, qy, qz, qw (7). */
  state: ReplayTrack;
  rotation: Fr3Rotation | null;
  action: ReplayTrack | null;
  actionMode: Fr3ReplayMode;
  actionRotation: Fr3Rotation | null;
  /** The observation gripper column, one value per row, or null. */
  gripper: ReplayTrack | null;
  /** Episode-relative seconds of the first and last row. */
  startS: number;
  endS: number;
  rows: number;
  /** True when the row count suggests the viewer thinned the episode. */
  maybeSampled: boolean;
  warnings: ReplayWarning[];
}

const hasNaN = (values: ArrayLike<number>, from: number, width: number) => {
  for (let c = 0; c < width; c++)
    if (!Number.isFinite(values[from + c])) return true;
  return false;
};

/**
 * Clamp 7-wide joint rows to the FR3 limits in place. A NaN stays NaN (the
 * scene holds the previous pose over it). Returns how many rows were clamped
 * and how many of those by more than the tolerance.
 */
export function clampJointRows(
  values: { length: number; [index: number]: number },
  tolerance = JOINT_LIMIT_TOLERANCE,
): { rows: number; farRows: number } {
  let rows = 0;
  let farRows = 0;
  for (let at = 0; at + 7 <= values.length; at += 7) {
    let clamped = false;
    let far = false;
    for (let c = 0; c < 7; c++) {
      const v = values[at + c];
      const { lower, upper } = FR3_JOINT_LIMITS[c];
      if (v < lower) {
        clamped = true;
        far ||= v < lower - tolerance;
        values[at + c] = lower;
      } else if (v > upper) {
        clamped = true;
        far ||= v > upper + tolerance;
        values[at + c] = upper;
      }
    }
    if (clamped) rows++;
    if (far) farRows++;
  }
  return { rows, farRows };
}

/**
 * Packs the detected columns of one episode. Null when the detection is
 * `none` or there are fewer than two usable rows (nothing to replay).
 * The joint columns of `state` and `action` are clamped to the FR3 limits;
 * the pose columns are used as they are.
 */
export function buildReplayData(
  detection: Fr3ReplayDetection,
  rows: readonly Record<string, number>[],
): ReplayData | null {
  if (detection.mode === "none") return null;
  const state = packTrack(rows, detection.stateCols);
  const n = state.t.length;
  if (n < 2) return null;
  const warnings: ReplayWarning[] = [];

  if (detection.mode === "joints") {
    const found = clampJointRows(state.values);
    if (found.rows > 0) {
      warnings.push({ kind: "joints-clamped", track: "state", ...found });
    }
  }
  let missing = 0;
  for (let i = 0; i < n; i++)
    if (hasNaN(state.values, i * state.width, state.width)) missing++;
  if (missing > 0) warnings.push({ kind: "missing-values", rows: missing });

  let action: ReplayTrack | null = null;
  if (detection.actionMode !== "none" && detection.actionCols.length > 0) {
    action = packTrack(rows, detection.actionCols);
    if (detection.actionMode === "joints") {
      const found = clampJointRows(action.values);
      if (found.rows > 0) {
        warnings.push({ kind: "joints-clamped", track: "action", ...found });
      }
    }
  }
  const gripper = detection.gripperCol
    ? packTrack(rows, [detection.gripperCol])
    : null;

  return {
    mode: detection.mode,
    state,
    rotation: detection.rotation,
    action,
    actionMode: action ? detection.actionMode : "none",
    actionRotation: action ? detection.actionRotation : null,
    gripper,
    startS: state.t[0],
    endS: state.t[n - 1],
    rows: n,
    maybeSampled: n >= SAMPLED_ROW_COUNT,
    warnings,
  };
}
