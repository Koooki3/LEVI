// Which flat chart columns can drive a 3D FR3 replay (design:
// lab/levi-hub/programs/levi2/REPLAY-3D-DESIGN-20261021.md §2.2).
//
// Input is what the episode viewer already has: `meta/info.json` (only
// `robot_type` is read) and the keys of one `flatChartData` row. Those keys
// are `"<feature> | <name>"` with the names from `info.features[key].names`
// (`fetch-data.ts` builds them the same way for v2 and v3), `"<feature> | <i>"`
// when a feature has no names, and a bare `"<feature>"` for a v3 scalar.
//
// Pure: no DOM, no fetch, no values. Joint limits are checked when frames are
// packed, not here, because this function never sees a value.
import { CHART_CONFIG } from "@/utils/constants";

const DELIMITER = CHART_CONFIG.SERIES_NAME_DELIMITER;

export type Fr3ReplayMode = "joints" | "pose" | "none";
/** `euler` is rx, ry, rz in radians, unwrapped, extrinsic XYZ (roll/pitch/yaw; `levi/conversion/raw.py`). */
export type Fr3Rotation = "euler" | "quaternion";
export type Fr3RobotFamily = "fr3" | "generic" | "other";

export interface Fr3ReplayInfo {
  robot_type?: string | null;
}

export interface Fr3ReplayDetection {
  mode: Fr3ReplayMode;
  /** The observation feature the state columns come from, e.g. `observation.state`. */
  stateFeature: string | null;
  /** Flat keys in drive order: joints 1..7, or x, y, z then rx, ry, rz / qx, qy, qz, qw. */
  stateCols: string[];
  /** How the `action` columns can be drawn: joints (a second arm) or pose (axes only). */
  actionMode: Fr3ReplayMode;
  actionCols: string[];
  /** An observation gripper column (command or measured width), or null when there is none. */
  gripperCol: string | null;
  rotation: Fr3Rotation | null;
  actionRotation: Fr3Rotation | null;
  /**
   * Always false: which frame a pose is in (flange `fr3_link8` or a TCP, and
   * relative to which base) has not been verified (design §2.1), so pose
   * mode is labelled "pose frame not verified" and never solved into joints.
   */
  poseFrameVerified: false;
  /** One English sentence for logs and tests; the UI shows its own translated text. */
  reason: string;
}

interface Column {
  name: string | null;
  key: string;
}

// joint_1..7, joint_0..6, joint1, fr3_joint1..7, panda_joint1..7, and the
// same with a `_pos` / `_position` / `.pos` suffix (AERI's joint_1_pos).
const JOINT_RE = /^(?:(?:fr3|panda)_)?joint_?(\d+)(?:[._]pos(?:ition)?)?$/i;
const GRIPPER_RE =
  /^(?:(?:fr3|panda|robotiq)_)?gripper(?:[._](?:width|pos|position|state|opening))?$/i;
const UNNAMED_RE = /^\d+$/;
const FR3_FAMILY = ["fr3", "franka", "panda"];
const GENERIC_ROBOT_TYPES = new Set(["", "generic", "generic_arm"]);
const STATE_FEATURE_PRIORITY = [
  "observation.joint_positions",
  "observation.state",
];
const ACTION_FEATURE = "action";

/**
 * FR3 family by substring (fr3, franka, panda); a generic arm, or nothing
 * recorded, may still be detected from column names; anything else (SO-101,
 * OpenArm, Unitree G1, ...) stays on its own viewer. The substring "g1" means
 * nothing here: `isG1Robot` would call `franka_g1x` a G1.
 */
export function fr3RobotFamily(
  robotType: string | null | undefined,
): Fr3RobotFamily {
  const lower = (robotType ?? "").trim().toLowerCase();
  if (FR3_FAMILY.some((word) => lower.includes(word))) return "fr3";
  if (GENERIC_ROBOT_TYPES.has(lower)) return "generic";
  return "other";
}

function groupByFeature(flatKeys: readonly string[]): Map<string, Column[]> {
  const features = new Map<string, Column[]>();
  for (const key of flatKeys) {
    const at = key.indexOf(DELIMITER);
    const feature = at < 0 ? key : key.slice(0, at);
    const name = at < 0 ? null : key.slice(at + DELIMITER.length);
    const columns = features.get(feature) ?? [];
    columns.push({ name, key });
    features.set(feature, columns);
  }
  return features;
}

function observationFeatures(features: Map<string, Column[]>): string[] {
  const first = STATE_FEATURE_PRIORITY.filter((f) => features.has(f));
  const rest = [...features.keys()]
    .filter((f) => f.startsWith("observation.") && !first.includes(f))
    .sort();
  return [...first, ...rest];
}

type Found<T> = { ok: true; value: T } | { ok: false; problem: string } | null;

function findJoints(feature: string, columns: Column[]): Found<string[]> {
  const byIndex = new Map<number, string>();
  let duplicate: number | null = null;
  for (const { name, key } of columns) {
    const match = name === null ? null : JOINT_RE.exec(name);
    if (!match) continue;
    const index = Number(match[1]);
    if (byIndex.has(index)) duplicate = index;
    else byIndex.set(index, key);
  }
  if (byIndex.size === 0) {
    // Seven or more index-named columns (`observation.state | 0`): info.json
    // had no names, so which index is a joint cannot be told.
    const unnamed =
      columns.length >= 7 &&
      columns.every(({ name }) => name !== null && UNNAMED_RE.test(name));
    if (!unnamed) return null;
    const problem = `${feature} columns are unnamed (no names in info.json)`;
    return { ok: false, problem };
  }
  if (duplicate !== null) {
    return { ok: false, problem: `${feature} names joint ${duplicate} twice` };
  }
  const indices = [...byIndex.keys()].sort((a, b) => a - b);
  const first = indices[0];
  if (
    indices.length === 7 &&
    (first === 0 || first === 1) &&
    indices[6] === first + 6
  ) {
    return { ok: true, value: indices.map((i) => byIndex.get(i)!) };
  }
  return {
    ok: false,
    problem: `${feature} has joints [${indices.join(", ")}], not 7 numbered 0..6 or 1..7`,
  };
}

function findPose(
  columns: Column[],
): { cols: string[]; rotation: Fr3Rotation } | null {
  const byName = new Map<string, string>();
  for (const { name, key } of columns) {
    if (name === null) continue;
    const lower = name.toLowerCase();
    if (!byName.has(lower)) byName.set(lower, key);
  }
  const pick = (names: string[]): string[] | null => {
    if (!names.every((n) => byName.has(n))) return null;
    return names.map((n) => byName.get(n)!);
  };
  const position = pick(["x", "y", "z"]);
  if (!position) return null;
  const euler = pick(["rx", "ry", "rz"]);
  if (euler) return { cols: [...position, ...euler], rotation: "euler" };
  const quaternion = pick(["qx", "qy", "qz", "qw"]);
  if (quaternion) {
    return { cols: [...position, ...quaternion], rotation: "quaternion" };
  }
  return null;
}

function findGripper(
  features: Map<string, Column[]>,
  ordered: string[],
): string | null {
  for (const feature of ordered) {
    for (const { name, key } of features.get(feature) ?? []) {
      if (name !== null && GRIPPER_RE.test(name)) return key;
    }
  }
  // A scalar feature such as `observation.gripper_width` (key without a
  // name in v3, `observation.gripper_width | 0` in v2).
  for (const feature of ordered) {
    const columns = features.get(feature) ?? [];
    const last = feature.slice(feature.lastIndexOf(".") + 1);
    if (columns.length === 1 && GRIPPER_RE.test(last)) return columns[0].key;
  }
  return null;
}

function none(reason: string): Fr3ReplayDetection {
  return {
    mode: "none",
    stateFeature: null,
    stateCols: [],
    actionMode: "none",
    actionCols: [],
    gripperCol: null,
    rotation: null,
    actionRotation: null,
    poseFrameVerified: false,
    reason,
  };
}

function detectAction(
  columns: Column[] | undefined,
): Pick<Fr3ReplayDetection, "actionMode" | "actionCols" | "actionRotation"> {
  const empty = {
    actionMode: "none" as const,
    actionCols: [],
    actionRotation: null,
  };
  if (!columns) return empty;
  const joints = findJoints(ACTION_FEATURE, columns);
  if (joints?.ok) {
    return {
      actionMode: "joints",
      actionCols: joints.value,
      actionRotation: null,
    };
  }
  const pose = findPose(columns);
  if (pose) {
    return {
      actionMode: "pose",
      actionCols: pose.cols,
      actionRotation: pose.rotation,
    };
  }
  return empty;
}

/**
 * Joints first (7 named joint columns in an observation feature), then pose
 * (x, y, z + rx, ry, rz or qx, qy, qz, qw), else none. Only the FR3 family
 * and generic arms are considered; a missing gripper column does not change
 * the mode.
 */
export function detectFr3Replay(
  info: Fr3ReplayInfo,
  flatKeys: readonly string[],
): Fr3ReplayDetection {
  const robotType = (info.robot_type ?? "").trim();
  const family = fr3RobotFamily(robotType);
  if (family === "other") {
    return none(
      `robot_type "${robotType}" is neither FR3/Franka/Panda nor a generic arm`,
    );
  }
  const features = groupByFeature(flatKeys);
  const ordered = observationFeatures(features);
  const action = detectAction(features.get(ACTION_FEATURE));
  const gripperCol = findGripper(features, ordered);
  const basis =
    family === "fr3"
      ? `robot_type "${robotType}" is FR3 family`
      : `robot_type "${robotType || "(none)"}" is a generic arm`;
  const problems: string[] = [];

  for (const feature of ordered) {
    const joints = findJoints(feature, features.get(feature)!);
    if (!joints) continue;
    if (!joints.ok) {
      problems.push(joints.problem);
      continue;
    }
    return {
      mode: "joints",
      stateFeature: feature,
      stateCols: joints.value,
      ...action,
      gripperCol,
      rotation: null,
      poseFrameVerified: false,
      reason: `${basis}; 7 joint columns in ${feature}`,
    };
  }

  for (const feature of ordered) {
    const pose = findPose(features.get(feature)!);
    if (!pose) continue;
    return {
      mode: "pose",
      stateFeature: feature,
      stateCols: pose.cols,
      ...action,
      gripperCol,
      rotation: pose.rotation,
      poseFrameVerified: false,
      reason: `${basis}; end-effector pose (${pose.rotation}) in ${feature}, pose frame not verified`,
    };
  }

  const detail = problems.length > 0 ? `: ${problems.join("; ")}` : "";
  return none(
    `${basis} but no 7 joint columns or x, y, z + rotation columns${detail}`,
  );
}
