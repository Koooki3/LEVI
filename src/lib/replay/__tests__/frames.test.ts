import { describe, expect, test } from "bun:test";
import { FR3_JOINT_LIMITS } from "@/lib/robot-studio/fr3-kinematics";
import { detectFr3Replay } from "../fr3-columns";
import {
  JOINT_LIMIT_TOLERANCE,
  SAMPLED_ROW_COUNT,
  buildReplayData,
  clampJointRows,
} from "../frames";

const keys = (feature: string, names: readonly string[]) =>
  names.map((name) => `${feature} | ${name}`);
const droid = [...Array.from({ length: 7 }, (_, i) => `joint_${i}`), "gripper"];
const pose = ["x", "y", "z", "rx", "ry", "rz", "gripper"];

/** Joint c at row i: the middle of its range plus 0.01 rad per row, inside the limits. */
const mid = (c: number) =>
  (FR3_JOINT_LIMITS[c].lower + FR3_JOINT_LIMITS[c].upper) / 2;
/** Column 7 is the gripper: i / 10. */
const jointValue = (i: number, c: number) =>
  c < 7 ? mid(c) + i * 0.01 : i / 10;
function jointRows(n: number, feature = "observation.state", names = droid) {
  return Array.from({ length: n }, (_, i) => {
    const row: Record<string, number> = { timestamp: i };
    names.forEach((name, c) => {
      row[`${feature} | ${name}`] = jointValue(i, c);
    });
    return row;
  });
}
const fr3Info = { robot_type: "franka_panda" };

describe("buildReplayData: joints", () => {
  test("packs state joints in joint order with the row timestamps", () => {
    const rows = jointRows(5);
    const flat = ["timestamp", ...keys("observation.state", droid)];
    const detection = detectFr3Replay(fr3Info, flat);
    const data = buildReplayData(detection, rows)!;
    expect(data.mode).toBe("joints");
    expect(data.state.width).toBe(7);
    expect(Array.from(data.state.t)).toEqual([0, 1, 2, 3, 4]);
    // row 3, joint index 2 (third joint)
    expect(data.state.values[3 * 7 + 2]).toBeCloseTo(jointValue(3, 2), 12);
    expect(data.rows).toBe(5);
    expect([data.startS, data.endS]).toEqual([0, 4]);
    expect(data.rotation).toBeNull();
    expect(data.warnings).toEqual([]);
  });

  test("the gripper is its own one-wide track and a missing action stays null", () => {
    const flat = ["timestamp", ...keys("observation.state", droid)];
    const data = buildReplayData(detectFr3Replay(fr3Info, flat), jointRows(4))!;
    expect(data.gripper!.width).toBe(1);
    expect(data.gripper!.values[2]).toBeCloseTo(jointValue(2, 7), 12);
    expect(data.action).toBeNull();
    expect(data.actionMode).toBe("none");
  });

  test("action joints are packed the same way and clamped on their own", () => {
    const rows = jointRows(3).map((row, i) => ({
      ...row,
      ...Object.fromEntries(
        droid.map((name, c) => [
          `action | ${name}`,
          c === 0 && i === 2 ? 9 : jointValue(i, c),
        ]),
      ),
    }));
    const flat = [
      "timestamp",
      ...keys("observation.state", droid),
      ...keys("action", droid),
    ];
    const data = buildReplayData(detectFr3Replay(fr3Info, flat), rows)!;
    expect(data.actionMode).toBe("joints");
    expect(data.action!.width).toBe(7);
    expect(data.action!.values[2 * 7]).toBe(FR3_JOINT_LIMITS[0].upper);
    expect(data.warnings).toEqual([
      { kind: "joints-clamped", track: "action", rows: 1, farRows: 1 },
    ]);
  });

  test("a row with no finite timestamp is dropped and rows are put in time order", () => {
    const rows = jointRows(4);
    rows[1] = { ...rows[1], timestamp: NaN };
    const swapped = [rows[0], rows[3], rows[2]];
    const flat = ["timestamp", ...keys("observation.state", droid)];
    const data = buildReplayData(detectFr3Replay(fr3Info, flat), swapped)!;
    expect(Array.from(data.state.t)).toEqual([0, 2, 3]);
  });

  test("a value that is not a number is NaN in the track and counted as missing", () => {
    const rows = jointRows(3);
    delete rows[1]["observation.state | joint_3"];
    const flat = ["timestamp", ...keys("observation.state", droid)];
    const data = buildReplayData(detectFr3Replay(fr3Info, flat), rows)!;
    expect(Number.isNaN(data.state.values[1 * 7 + 3])).toBe(true);
    expect(data.warnings).toEqual([{ kind: "missing-values", rows: 1 }]);
  });
});

describe("buildReplayData: pose", () => {
  const flat = ["timestamp", ...keys("observation.state", pose)];
  const rows = Array.from({ length: 6 }, (_, i) => ({
    timestamp: i * 0.1,
    "observation.state | x": 0.3 + i * 0.01,
    "observation.state | y": 0,
    "observation.state | z": 0.4,
    "observation.state | rx": 3.0 + i,
    "observation.state | ry": 0.1,
    "observation.state | rz": -0.2,
    "observation.state | gripper": i < 3 ? 1 : 0,
  }));

  test("a generic arm gives pose mode with 6 columns, unclamped, rotation euler", () => {
    const detection = detectFr3Replay({ robot_type: "generic_arm" }, flat);
    const data = buildReplayData(detection, rows)!;
    expect(data.mode).toBe("pose");
    expect(data.rotation).toBe("euler");
    expect(data.state.width).toBe(6);
    // rx runs past pi (unwrapped): left as it is, never clamped as a joint.
    expect(data.state.values[5 * 6 + 3]).toBe(8);
    expect(data.warnings).toEqual([]);
    expect(Array.from(data.gripper!.values)).toEqual([1, 1, 1, 0, 0, 0]);
  });

  test("quaternion pose columns pack to width 7", () => {
    const names = ["x", "y", "z", "qx", "qy", "qz", "qw"];
    const quat = rows.map((row, i) => ({
      timestamp: row.timestamp,
      "observation.state | x": i,
      "observation.state | y": 0,
      "observation.state | z": 0,
      "observation.state | qx": 0,
      "observation.state | qy": 0,
      "observation.state | qz": 0,
      "observation.state | qw": 1,
    }));
    const detection = detectFr3Replay({ robot_type: "" }, [
      "timestamp",
      ...keys("observation.state", names),
    ]);
    const data = buildReplayData(detection, quat)!;
    expect(data.rotation).toBe("quaternion");
    expect(data.state.width).toBe(7);
    expect(data.state.values[4 * 7]).toBe(4);
    expect(data.state.values[4 * 7 + 6]).toBe(1);
  });

  test("an action given as pose stays pose even when the state is joints", () => {
    const withAction = rows.map((row) => ({
      ...row,
      "action | x": 0.5,
      "action | y": 0,
      "action | z": 0.5,
      "action | rx": 0,
      "action | ry": 0,
      "action | rz": 0,
    }));
    const detection = detectFr3Replay({ robot_type: "generic_arm" }, [
      ...flat,
      ...keys("action", ["x", "y", "z", "rx", "ry", "rz"]),
    ]);
    const data = buildReplayData(detection, withAction)!;
    expect(data.actionMode).toBe("pose");
    expect(data.actionRotation).toBe("euler");
    expect(data.action!.width).toBe(6);
  });
});

describe("buildReplayData: nothing to replay", () => {
  test("mode none gives null", () => {
    const detection = detectFr3Replay({ robot_type: "so101_follower" }, [
      "timestamp",
      ...keys("observation.state", droid),
    ]);
    expect(detection.mode).toBe("none");
    expect(buildReplayData(detection, jointRows(5))).toBeNull();
  });

  test("fewer than two usable rows gives null", () => {
    const flat = ["timestamp", ...keys("observation.state", droid)];
    const detection = detectFr3Replay(fr3Info, flat);
    expect(buildReplayData(detection, jointRows(1))).toBeNull();
    expect(buildReplayData(detection, [])).toBeNull();
  });

  test("an episode of SAMPLED_ROW_COUNT rows is marked as maybe thinned", () => {
    const flat = ["timestamp", ...keys("observation.state", droid)];
    const detection = detectFr3Replay(fr3Info, flat);
    expect(
      buildReplayData(detection, jointRows(SAMPLED_ROW_COUNT))!.maybeSampled,
    ).toBe(true);
    expect(
      buildReplayData(detection, jointRows(SAMPLED_ROW_COUNT - 1))!
        .maybeSampled,
    ).toBe(false);
  });
});

describe("clampJointRows", () => {
  const [j1, , , j4] = FR3_JOINT_LIMITS;
  const middle = () => Float64Array.from({ length: 7 }, (_, c) => mid(c));
  test("a value inside the tolerance is clamped without being counted as far", () => {
    const values = new Float64Array(14);
    values.set(middle(), 0);
    values.set(middle(), 7);
    values[0] = j1.upper + JOINT_LIMIT_TOLERANCE / 2;
    values[3] = j4.lower - 0.01;
    expect(clampJointRows(values)).toEqual({ rows: 1, farRows: 0 });
    expect(values[0]).toBe(j1.upper);
    expect(values[3]).toBe(j4.lower);
  });

  test("a value far outside (degrees read as radians) is clamped and counted as far", () => {
    const values = middle();
    values[0] = 90;
    expect(clampJointRows(values)).toEqual({ rows: 1, farRows: 1 });
    expect(values[0]).toBe(j1.upper);
  });

  test("NaN is left alone and never counted", () => {
    const values = new Float64Array(7).fill(NaN);
    expect(clampJointRows(values)).toEqual({ rows: 0, farRows: 0 });
    expect(values.every(Number.isNaN)).toBe(true);
  });

  test("values exactly on a limit are not clamped", () => {
    const values = middle();
    FR3_JOINT_LIMITS.forEach(({ lower }, c) => (values[c] = lower));
    expect(clampJointRows(values)).toEqual({ rows: 0, farRows: 0 });
  });
});
