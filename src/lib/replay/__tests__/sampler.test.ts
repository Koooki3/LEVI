import { describe, expect, test } from "bun:test";
import {
  FR3_JOINT_LIMITS,
  forwardPose,
} from "@/lib/robot-studio/fr3-kinematics";
import { detectFr3Replay } from "../fr3-columns";
import { buildReplayData, type ReplayData } from "../frames";
import { quaternionFromRpy } from "../pose";
import { MAX_TRAIL_POINTS, createReplaySampler } from "../sampler";

const keys = (feature: string, names: readonly string[]) =>
  names.map((name) => `${feature} | ${name}`);
const jointNames = Array.from({ length: 7 }, (_, i) => `joint_${i + 1}`);
const mid = (c: number) =>
  (FR3_JOINT_LIMITS[c].lower + FR3_JOINT_LIMITS[c].upper) / 2;
/** Row i, joint c: its range's middle plus 0.05 rad per row (so rows are distinct). */
const joint = (i: number, c: number) => mid(c) + i * 0.05;

function jointsData(
  n: number,
  { dt = 0.1, action = false, gripper = false } = {},
): ReplayData {
  const rows = Array.from({ length: n }, (_, i) => {
    const row: Record<string, number> = { timestamp: i * dt };
    jointNames.forEach((name, c) => {
      row[`observation.state | ${name}`] = joint(i, c);
      if (action) row[`action | ${name}`] = joint(i + 1, c);
    });
    if (gripper) row["observation.state | gripper"] = i < 3 ? 1 : 0;
    return row;
  });
  const flat = [
    "timestamp",
    ...keys("observation.state", [
      ...jointNames,
      ...(gripper ? ["gripper"] : []),
    ]),
    ...(action ? keys("action", jointNames) : []),
  ];
  return buildReplayData(detectFr3Replay({ robot_type: "fr3" }, flat), rows)!;
}

const poseNames = ["x", "y", "z", "rx", "ry", "rz"];
function poseData(n: number, extra: Record<string, number>[] = []): ReplayData {
  const rows = Array.from({ length: n }, (_, i) => ({
    timestamp: i * 0.1,
    "observation.state | x": 0.3 + i * 0.01,
    "observation.state | y": -0.1,
    "observation.state | z": 0.4,
    "observation.state | rx": Math.PI / 2,
    "observation.state | ry": 0,
    "observation.state | rz": i === 1 ? NaN : i * 0.2,
    ...(extra[i] ?? {}),
  }));
  const flat = ["timestamp", ...keys("observation.state", poseNames)];
  return buildReplayData(
    detectFr3Replay({ robot_type: "generic_arm" }, flat),
    rows,
  )!;
}

describe("sampler: joints", () => {
  test("at a row's timestamp the joints are that row, and the index is its row", () => {
    const sampler = createReplaySampler(jointsData(6));
    const frame = sampler.at(3 * 0.1); // the row's own timestamp, bit for bit
    expect(frame.index).toBe(3);
    jointNames.forEach((_, c) =>
      expect(frame.joints[c]).toBeCloseTo(joint(3, c), 12),
    );
  });

  test("between two rows the joints are linearly interpolated", () => {
    const sampler = createReplaySampler(jointsData(6));
    const frame = sampler.at(0.25);
    expect(frame.index).toBe(2);
    jointNames.forEach((_, c) =>
      expect(frame.joints[c]).toBeCloseTo((joint(2, c) + joint(3, c)) / 2, 12),
    );
  });

  test("by timestamp, not by frame/fps: non-uniform rows give the row the time falls in", () => {
    const data = jointsData(4);
    // Move the rows to 0, 1, 1.1, 5 s (a thinned episode).
    (data.state.t as Float64Array).set([0, 1, 1.1, 5]);
    const sampler = createReplaySampler(data);
    expect(sampler.at(1.05).index).toBe(1);
    expect(sampler.at(4.9).index).toBe(2);
    expect(sampler.at(5).joints[0]).toBeCloseTo(joint(3, 0), 12);
  });

  test("before the first and after the last row it holds the end rows", () => {
    const sampler = createReplaySampler(jointsData(4));
    expect(sampler.at(-3).joints[0]).toBeCloseTo(joint(0, 0), 12);
    expect(sampler.at(99).joints[0]).toBeCloseTo(joint(3, 0), 12);
    expect(sampler.at(99).index).toBe(3);
  });

  test("the end effector is the flange from forward kinematics of those joints", () => {
    const sampler = createReplaySampler(jointsData(6));
    const frame = sampler.at(0.2);
    const expected = forwardPose(Array.from(frame.joints));
    expected.position.forEach((v, i) =>
      expect(frame.position[i]).toBeCloseTo(v, 12),
    );
    expected.quaternion.forEach((v, i) =>
      expect(frame.quaternion[i]).toBeCloseTo(v, 12),
    );
  });

  test("a hole in a row keeps the previous joints", () => {
    const data = jointsData(4);
    const sampler = createReplaySampler(data);
    sampler.at(0.1);
    const held = Array.from(sampler.frame.joints);
    (data.state.values as Float64Array)[2 * 7 + 4] = NaN;
    sampler.at(0.2);
    expect(Array.from(sampler.frame.joints)).toEqual(held);
  });

  test("a joint interpolated between two values on its limit stays inside the limit", () => {
    const data = jointsData(3);
    const values = data.state.values as Float64Array;
    for (let i = 0; i < 3; i++) values[i * 7] = FR3_JOINT_LIMITS[0].upper;
    const sampler = createReplaySampler(data);
    for (const t of [0.01, 0.07, 0.123456789, 0.19]) {
      expect(() => sampler.at(t)).not.toThrow();
      expect(sampler.frame.joints[0]).toBeLessThanOrEqual(
        FR3_JOINT_LIMITS[0].upper,
      );
    }
  });

  test("the action joints are sampled the same way, into their own buffer", () => {
    const sampler = createReplaySampler(jointsData(6, { action: true }));
    const frame = sampler.at(3 * 0.1);
    jointNames.forEach((_, c) => {
      expect(frame.actionJoints[c]).toBeCloseTo(joint(4, c), 12);
      expect(frame.joints[c]).toBeCloseTo(joint(3, c), 12);
    });
  });

  test("the gripper is a step: 1 until the row it changes at, never 0.5", () => {
    const sampler = createReplaySampler(jointsData(6, { gripper: true }));
    expect(sampler.at(0.15).gripper).toBe(1);
    expect(sampler.at(0.29).gripper).toBe(1);
    expect(sampler.at(3 * 0.1).gripper).toBe(0);
    expect(sampler.at(0.45).gripper).toBe(0);
  });

  test("no gripper column gives null", () => {
    expect(createReplaySampler(jointsData(3)).at(0.1).gripper).toBeNull();
  });
});

describe("sampler: pose", () => {
  test("position is interpolated, orientation is the raw.py roll/pitch/yaw rotation", () => {
    const sampler = createReplaySampler(poseData(4));
    const frame = sampler.at(0.25);
    expect(frame.position[0]).toBeCloseTo(0.325, 12);
    // At 0.2 s: rx = pi/2, rz = 0.4, as three-order ZYX / raw.py extrinsic XYZ.
    sampler.at(0.2);
    const expected = quaternionFromRpy(Math.PI / 2, 0, 0.4);
    expected.forEach((v, i) =>
      expect(sampler.frame.quaternion[i]).toBeCloseTo(v, 12),
    );
  });

  test("an unwrapped angle step across pi is not blended through the short way", () => {
    const rows = [
      { timestamp: 0, "observation.state | rx": 3.1 },
      { timestamp: 1, "observation.state | rx": -3.1 },
    ].map((row) => ({
      "observation.state | x": 0,
      "observation.state | y": 0,
      "observation.state | z": 0,
      "observation.state | ry": 0,
      "observation.state | rz": 0,
      ...row,
    }));
    const flat = ["timestamp", ...keys("observation.state", poseNames)];
    const data = buildReplayData(
      detectFr3Replay({ robot_type: "" }, flat),
      rows,
    )!;
    const sampler = createReplaySampler(data);
    // Rows 6.2 rad apart (a missed wrap): held at the earlier row, not blended to 0.
    sampler.at(0.5);
    const expected = quaternionFromRpy(3.1, 0, 0);
    expected.forEach((v, i) =>
      expect(sampler.frame.quaternion[i]).toBeCloseTo(v, 12),
    );
  });

  test("a hole in a pose row holds the previous pose", () => {
    const sampler = createReplaySampler(poseData(4));
    sampler.at(0);
    const held = Array.from(sampler.frame.quaternion);
    sampler.at(0.1); // row 1 has rz = NaN
    expect(Array.from(sampler.frame.quaternion)).toEqual(held);
  });

  test("quaternion columns are normalised and stepped", () => {
    const rows = [0, 1, 2].map((i) => ({
      timestamp: i,
      "observation.state | x": i,
      "observation.state | y": 0,
      "observation.state | z": 0,
      "observation.state | qx": 0,
      "observation.state | qy": 0,
      "observation.state | qz": i === 0 ? 0 : 2,
      "observation.state | qw": i === 0 ? 2 : 0,
    }));
    const flat = [
      "timestamp",
      ...keys("observation.state", ["x", "y", "z", "qx", "qy", "qz", "qw"]),
    ];
    const data = buildReplayData(
      detectFr3Replay({ robot_type: "generic_arm" }, flat),
      rows,
    )!;
    const sampler = createReplaySampler(data);
    expect(Array.from(sampler.at(0.5).quaternion)).toEqual([0, 0, 0, 1]);
    expect(sampler.frame.position[0]).toBeCloseTo(0.5, 12);
    expect(Array.from(sampler.at(1.5).quaternion)).toEqual([0, 0, 1, 0]);
  });

  test("an action given as a pose fills the action pose", () => {
    const extra = Array.from({ length: 4 }, (_, i) => ({
      "action | x": 1 + i,
      "action | y": 0,
      "action | z": 0,
      "action | rx": 0,
      "action | ry": 0,
      "action | rz": 0,
    }));
    const rows = Array.from({ length: 4 }, (_, i) => ({
      timestamp: i * 0.1,
      "observation.state | x": 0,
      "observation.state | y": 0,
      "observation.state | z": 0,
      "observation.state | rx": 0,
      "observation.state | ry": 0,
      "observation.state | rz": 0,
      ...extra[i],
    }));
    const flat = [
      "timestamp",
      ...keys("observation.state", poseNames),
      ...keys("action", poseNames),
    ];
    const data = buildReplayData(
      detectFr3Replay({ robot_type: "generic_arm" }, flat),
      rows,
    )!;
    const frame = createReplaySampler(data).at(0.2);
    expect(frame.actionPosition[0]).toBeCloseTo(3, 12);
  });
});

describe("trail and path", () => {
  test("trailRange is the last MAX_TRAIL_POINTS rows up to and including the index", () => {
    const sampler = createReplaySampler(poseData(1000));
    expect(sampler.trailRange(5)).toEqual({ from: 0, to: 6 });
    expect(sampler.trailRange(299)).toEqual({ from: 0, to: 300 });
    expect(sampler.trailRange(300)).toEqual({ from: 1, to: 301 });
    expect(sampler.trailRange(999)).toEqual({ from: 700, to: 1000 });
    const { from, to } = sampler.trailRange(999);
    expect(to - from).toBe(MAX_TRAIL_POINTS);
  });

  test("the path is one x, y, z per row: the columns in pose mode, forward kinematics in joints mode", () => {
    const pose = createReplaySampler(poseData(5)).path();
    expect(pose.length).toBe(15);
    expect(pose[3 * 3]).toBeCloseTo(0.33, 6);
    const data = jointsData(4);
    const path = createReplaySampler(data).path();
    const row2 = Array.from({ length: 7 }, (_, c) => joint(2, c));
    forwardPose(row2).position.forEach((v, i) =>
      expect(path[2 * 3 + i]).toBeCloseTo(v, 5),
    );
  });

  test("path is computed once", () => {
    const sampler = createReplaySampler(poseData(5));
    expect(sampler.path()).toBe(sampler.path());
  });
});
