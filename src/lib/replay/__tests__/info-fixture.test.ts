import { describe, expect, test } from "bun:test";
import { FR3_JOINT_LIMITS } from "@/lib/robot-studio/fr3-kinematics";
import { detectFr3Replay } from "../fr3-columns";
import { buildReplayData } from "../frames";
import { replayKind } from "../replay-kind";
import { createReplaySampler } from "../sampler";

// A hand-written `meta/info.json` in the layout a conversion that exports the
// robot's measured joints writes: pose and the gripper command in
// `observation.state`, the seven FR3 joints in `observation.joint_positions`,
// the measured opening in `observation.gripper_width`, and the pose again as
// the action. It is a fixture, not the converter's output: nothing here
// imports or runs the converter, so this test says what the viewer needs of
// such a dataset, whichever code writes it.
const jointNames = Array.from({ length: 7 }, (_, i) => `joint_${i + 1}`);
const poseNames = ["x", "y", "z", "rx", "ry", "rz"];
const infoJson = {
  codebase_version: "v2.1",
  robot_type: "franka_fr3",
  fps: 30,
  features: {
    "observation.images.hand": {
      dtype: "video",
      shape: [480, 640, 3],
      names: ["height", "width", "channel"],
    },
    "observation.state": {
      dtype: "float32",
      shape: [7],
      names: [...poseNames, "gripper"],
    },
    "observation.joint_positions": {
      dtype: "float32",
      shape: [7],
      names: jointNames,
    },
    "observation.gripper_width": {
      dtype: "float32",
      shape: [1],
      names: ["gripper_width"],
    },
    action: {
      dtype: "float32",
      shape: [7],
      names: [...poseNames, "gripper"],
    },
    timestamp: { dtype: "float32", shape: [1], names: null },
    frame_index: { dtype: "int64", shape: [1], names: null },
    episode_index: { dtype: "int64", shape: [1], names: null },
    index: { dtype: "int64", shape: [1], names: null },
    task_index: { dtype: "int64", shape: [1], names: null },
  },
} as const;

/**
 * The flat column keys the viewer builds from `info.features` (as
 * `fetch-data.ts` does for v2.x): numeric one-dimensional features, one key
 * `"<feature> | <name>"` per entry of `names`. The excluded bookkeeping
 * columns (`EXCLUDED_COLUMNS`) are left out, `timestamp` is kept as the viewer
 * adds it.
 */
function flatKeysOfInfo(info: typeof infoJson): string[] {
  const excluded = ["frame_index", "episode_index", "index", "task_index"];
  const keys = ["timestamp"];
  for (const [key, feature] of Object.entries(info.features)) {
    if (feature.dtype === "video" || feature.shape.length !== 1) continue;
    if (key === "timestamp" || excluded.includes(key)) continue;
    const names = feature.names as readonly string[] | null;
    if (Array.isArray(names)) {
      for (const name of names) keys.push(`${key} | ${name}`);
    } else {
      for (let i = 0; i < feature.shape[0]; i++) keys.push(`${key} | ${i}`);
    }
  }
  return keys;
}

const mid = (c: number) =>
  (FR3_JOINT_LIMITS[c].lower + FR3_JOINT_LIMITS[c].upper) / 2;
/** Rows as `flatChartData` has them: flat key to number, plus `timestamp`. */
function rows(n: number) {
  return Array.from({ length: n }, (_, i) => {
    const row: Record<string, number> = { timestamp: i / 30 };
    poseNames.forEach((name, c) => {
      row[`observation.state | ${name}`] = 0.3 + c * 0.01 + i * 0.001;
      row[`action | ${name}`] = 0.3 + c * 0.01 + (i + 1) * 0.001;
    });
    row["observation.state | gripper"] = i < 5 ? 1 : 0;
    row["action | gripper"] = i < 5 ? 1 : 0;
    jointNames.forEach((name, c) => {
      row[`observation.joint_positions | ${name}`] = mid(c) + i * 0.01;
    });
    row["observation.gripper_width | gripper_width"] = i < 5 ? 0.08 : 0;
    return row;
  });
}

describe("a dataset with measured joints, from a hand-written info.json", () => {
  const flat = flatKeysOfInfo(infoJson);

  test("the flat keys are what the detection expects", () => {
    expect(flat).toContain("observation.joint_positions | joint_1");
    expect(flat).toContain("observation.joint_positions | joint_7");
    expect(flat).toContain("observation.gripper_width | gripper_width");
    expect(flat).not.toContain("observation.images.hand | height");
  });

  test("detectFr3Replay: FR3 joints mode, from observation.joint_positions, with the measured width", () => {
    const result = detectFr3Replay(infoJson, flat);
    expect(result.mode).toBe("joints");
    expect(result.stateFeature).toBe("observation.joint_positions");
    expect(result.stateCols).toEqual(
      jointNames.map((name) => `observation.joint_positions | ${name}`),
    );
    expect(result.gripperCol).toBe("observation.gripper_width | gripper_width");
    expect(result.gripperKind).toBe("width");
    expect(result.rotation).toBeNull();
    // The action of this layout is the end-effector pose, drawn as axes.
    expect(result.actionMode).toBe("pose");
    expect(result.actionRotation).toBe("euler");
  });

  test("replayKind sends it to the Episodes panel as an FR3, not to the URDF tab", () => {
    const kind = replayKind(infoJson.robot_type, flat);
    expect(kind.kind).toBe("panel");
    if (kind.kind !== "panel") return;
    expect(kind.family).toBe("fr3");
    expect(kind.detection.mode).toBe("joints");
  });

  test("its rows pack and sample: seven joint columns in joint order, the width as a measured value", () => {
    const detection = detectFr3Replay(infoJson, flat);
    const data = buildReplayData(detection, rows(10))!;
    expect(data.mode).toBe("joints");
    expect(data.rows).toBe(10);
    expect(data.state.width).toBe(7);
    expect(data.gripperKind).toBe("width");
    expect(data.warnings).toEqual([]);
    const frame = createReplaySampler(data).at(4 / 30);
    jointNames.forEach((_, c) =>
      expect(frame.joints[c]).toBeCloseTo(mid(c) + 4 * 0.01, 12),
    );
    expect(frame.gripper).toBeCloseTo(0.08, 12);
  });

  test("the same columns with robot_type generic_arm are not drawn as an FR3 arm", () => {
    // Before the converter named the robot franka_fr3 (and for any 7-joint
    // arm that also says joint_1..7) the joints must not become an FR3.
    const result = detectFr3Replay(
      { ...infoJson, robot_type: "generic_arm" },
      flat,
    );
    expect(result.mode).toBe("pose");
    expect(result.stateFeature).toBe("observation.state");
  });
});
