import { describe, expect, test } from "bun:test";
import { detectFr3Replay, fr3RobotFamily } from "@/lib/replay/fr3-columns";
import { isG1Robot } from "@/lib/so101-robot";

// Flat keys the way fetch-data.ts builds them from info.features[key].names:
// "<feature> | <name>", plus the bare "timestamp" column.
const keys = (feature: string, names: readonly string[]) =>
  names.map((name) => `${feature} | ${name}`);
const range = (from: number, count: number) =>
  Array.from({ length: count }, (_, i) => from + i);
const droidNames = [...range(0, 7).map((i) => `joint_${i}`), "gripper"];
const poseEuler = ["x", "y", "z", "rx", "ry", "rz", "gripper"];

describe("detectFr3Replay: joints", () => {
  test("DROID view (franka_panda, joint_0..6 + gripper)", () => {
    const flat = [
      "timestamp",
      ...keys("observation.state", droidNames),
      ...keys("action", droidNames),
    ];
    const result = detectFr3Replay({ robot_type: "franka_panda" }, flat);
    expect(result.mode).toBe("joints");
    expect(result.stateFeature).toBe("observation.state");
    expect(result.stateCols).toEqual(
      keys("observation.state", droidNames.slice(0, 7)),
    );
    expect(result.actionMode).toBe("joints");
    expect(result.actionCols).toEqual(keys("action", droidNames.slice(0, 7)));
    expect(result.gripperCol).toBe("observation.state | gripper");
    expect(result.rotation).toBeNull();
  });

  test("AERI names joint_1_pos..joint_7_pos, given out of order, come back in joint order", () => {
    const names = range(1, 7).map((i) => `joint_${i}_pos`);
    const shuffled = [3, 0, 6, 2, 5, 1, 4].map((i) => names[i]);
    const result = detectFr3Replay(
      { robot_type: "fr3" },
      keys("observation.state", shuffled),
    );
    expect(result.mode).toBe("joints");
    expect(result.stateCols).toEqual(keys("observation.state", names));
  });

  test("fr3_joint1..7 drive the arm when robot_type is fr3", () => {
    const names = range(1, 7).map((i) => `fr3_joint${i}`);
    const result = detectFr3Replay(
      { robot_type: "fr3" },
      keys("observation.state", names),
    );
    expect(result.mode).toBe("joints");
    expect(result.stateCols).toEqual(keys("observation.state", names));
  });

  test("a missing gripper column keeps the joints mode", () => {
    const result = detectFr3Replay(
      { robot_type: "franka_panda" },
      keys("observation.state", droidNames.slice(0, 7)),
    );
    expect(result.mode).toBe("joints");
    expect(result.gripperCol).toBeNull();
    expect(result.actionMode).toBe("none");
    expect(result.actionCols).toEqual([]);
  });

  test("a scalar gripper feature is found in v3 (bare key) and v2 (| 0) form", () => {
    const joints = keys("observation.state", droidNames.slice(0, 7));
    const v3 = detectFr3Replay({ robot_type: "fr3" }, [
      ...joints,
      "observation.gripper_width",
    ]);
    expect(v3.gripperCol).toBe("observation.gripper_width");
    const v2 = detectFr3Replay({ robot_type: "fr3" }, [
      ...joints,
      "observation.gripper_width | 0",
    ]);
    expect(v2.gripperCol).toBe("observation.gripper_width | 0");
  });

  test("exported joints (observation.joint_positions) win over a pose state", () => {
    const names = range(1, 7).map((i) => `joint_${i}`);
    const result = detectFr3Replay({ robot_type: "fr3" }, [
      ...keys("observation.state", poseEuler),
      ...keys("observation.joint_positions", names),
      ...keys("action", poseEuler),
    ]);
    expect(result.mode).toBe("joints");
    expect(result.stateFeature).toBe("observation.joint_positions");
    expect(result.gripperCol).toBe("observation.state | gripper");
    expect(result.actionMode).toBe("pose");
    expect(result.actionRotation).toBe("euler");
  });
});

describe("detectFr3Replay: generic arms never get joints", () => {
  // xArm7, Kinova Gen3 and others also name 7 joints joint_1..7: without an
  // FR3/Franka robot_type the arm would be drawn as a wrong but plausible FR3.
  const joints = keys(
    "observation.state",
    range(1, 7).map((i) => `joint_${i}`),
  );
  const undeclared = "robot_type does not declare an FR3/Franka";

  test("fr3_joint1..7 on generic_arm: no pose columns, so none", () => {
    const names = range(1, 7).map((i) => `fr3_joint${i}`);
    const result = detectFr3Replay(
      { robot_type: "generic_arm" },
      keys("observation.state", names),
    );
    expect(result.mode).toBe("none");
    expect(result.stateCols).toEqual([]);
    expect(result.reason).toContain(undeclared);
  });

  test("empty robot_type with joint_1..7 only: none, with the reason", () => {
    for (const robotType of ["", null, undefined, "generic"]) {
      const result = detectFr3Replay({ robot_type: robotType }, joints);
      expect(result.mode).toBe("none");
      expect(result.reason).toContain(undeclared);
      expect(result.reason).toContain("set robot_type to show the arm");
    }
  });

  test("empty robot_type with joint_1..7 and a pose: pose", () => {
    const result = detectFr3Replay({ robot_type: "" }, [
      ...joints,
      ...keys("observation.ee_pose", poseEuler),
    ]);
    expect(result.mode).toBe("pose");
    expect(result.stateFeature).toBe("observation.ee_pose");
    expect(result.reason).toContain(undeclared);
  });

  test("generic_arm with joints and pose in one feature, joint action: pose, no joint shadow", () => {
    const names = [...range(1, 7).map((i) => `joint_${i}`), ...poseEuler];
    const result = detectFr3Replay({ robot_type: "generic_arm" }, [
      ...keys("observation.state", names),
      ...keys("action", range(1, 7).map((i) => `joint_${i}`)),
    ]);
    expect(result.mode).toBe("pose");
    expect(result.rotation).toBe("euler");
    expect(result.stateCols).toEqual(
      keys("observation.state", poseEuler.slice(0, 6)),
    );
    expect(result.actionMode).toBe("none");
    expect(result.actionCols).toEqual([]);
  });

  test("the same joint columns with robot_type fr3 or franka_panda do drive the arm", () => {
    for (const robotType of ["fr3", "franka_panda"]) {
      const result = detectFr3Replay({ robot_type: robotType }, joints);
      expect(result.mode).toBe("joints");
      expect(result.stateCols).toEqual(joints);
    }
  });
});

describe("detectFr3Replay: pose", () => {
  test("robot_capture conversion (generic_arm, x..rz + gripper)", () => {
    const result = detectFr3Replay({ robot_type: "generic_arm" }, [
      "timestamp",
      ...keys("observation.state", poseEuler),
      ...keys("action", poseEuler),
    ]);
    expect(result.mode).toBe("pose");
    expect(result.rotation).toBe("euler");
    expect(result.stateCols).toEqual(
      keys("observation.state", poseEuler.slice(0, 6)),
    );
    expect(result.actionMode).toBe("pose");
    expect(result.actionCols).toEqual(keys("action", poseEuler.slice(0, 6)));
    expect(result.gripperCol).toBe("observation.state | gripper");
    expect(result.poseFrameVerified).toBe(false);
    expect(result.reason).toContain("pose frame not verified");
  });

  test("quaternion pose comes back as x, y, z, qx, qy, qz, qw", () => {
    const listed = ["qw", "x", "qx", "y", "qy", "z", "qz", "gripper"];
    const result = detectFr3Replay(
      { robot_type: "generic_arm" },
      keys("observation.state", listed),
    );
    expect(result.mode).toBe("pose");
    expect(result.rotation).toBe("quaternion");
    expect(result.stateCols).toEqual(
      keys("observation.state", ["x", "y", "z", "qx", "qy", "qz", "qw"]),
    );
  });

  test("seven joints win over a pose in the same feature", () => {
    const names = [...droidNames.slice(0, 7), "x", "y", "z", "rx", "ry", "rz"];
    const result = detectFr3Replay(
      { robot_type: "fr3" },
      keys("observation.state", names),
    );
    expect(result.mode).toBe("joints");
  });

  test("position without rotation is not a pose", () => {
    const result = detectFr3Replay(
      { robot_type: "generic_arm" },
      keys("observation.state", ["x", "y", "z", "gripper"]),
    );
    expect(result.mode).toBe("none");
  });
});

describe("detectFr3Replay: none", () => {
  test("another robot type stays off even with FR3-like joint names", () => {
    for (const robotType of ["so101", "koch", "openarm", "unitree_g1"]) {
      const result = detectFr3Replay(
        { robot_type: robotType },
        keys("observation.state", droidNames),
      );
      expect(result.mode).toBe("none");
      expect(result.reason).toContain(robotType);
    }
  });

  test("a name containing g1 is not read as a G1 (isG1Robot would)", () => {
    expect(isG1Robot("franka_g1x")).toBe(true);
    expect(fr3RobotFamily("franka_g1x")).toBe("fr3");
    const result = detectFr3Replay(
      { robot_type: "franka_g1x" },
      keys("observation.state", droidNames),
    );
    expect(result.mode).toBe("joints");
    expect(fr3RobotFamily("unitree_g1")).toBe("other");
    expect(fr3RobotFamily("g1")).toBe("other");
  });

  test("robot type family: case, blanks and missing values", () => {
    expect(fr3RobotFamily("Franka_Panda")).toBe("fr3");
    expect(fr3RobotFamily(" FR3 ")).toBe("fr3");
    expect(fr3RobotFamily("generic_arm")).toBe("generic");
    expect(fr3RobotFamily("")).toBe("generic");
    expect(fr3RobotFamily(null)).toBe("generic");
    expect(fr3RobotFamily(undefined)).toBe("generic");
  });

  test("eight joints (joint_0..joint_7) are not a 7-joint arm", () => {
    const result = detectFr3Replay(
      { robot_type: "fr3" },
      keys("observation.state", range(0, 8).map((i) => `joint_${i}`)),
    );
    expect(result.mode).toBe("none");
    expect(result.reason).toContain("not 7 numbered");
  });

  test("the same joint named twice is refused, not guessed", () => {
    const names = [...range(1, 7).map((i) => `joint_${i}`), "joint_1_pos"];
    const result = detectFr3Replay(
      { robot_type: "fr3" },
      keys("observation.state", names),
    );
    expect(result.mode).toBe("none");
    expect(result.reason).toContain("twice");
  });

  test("unnamed columns (observation.state | 0..7) are not guessed as joints", () => {
    const result = detectFr3Replay(
      { robot_type: "franka_panda" },
      keys("observation.state", range(0, 8).map(String)),
    );
    expect(result.mode).toBe("none");
    expect(result.reason).toContain("unnamed");
  });

  test("joints only in action do not drive the state arm", () => {
    const result = detectFr3Replay(
      { robot_type: "fr3" },
      keys("action", droidNames),
    );
    expect(result.mode).toBe("none");
  });

  test("no columns at all", () => {
    const result = detectFr3Replay({ robot_type: "fr3" }, []);
    expect(result.mode).toBe("none");
    expect(result.stateCols).toEqual([]);
    expect(result.gripperCol).toBeNull();
  });
});
