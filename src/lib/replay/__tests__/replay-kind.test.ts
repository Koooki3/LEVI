import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { flatKeysOf, replayKind } from "../replay-kind";

const keys = (feature: string, names: readonly string[]) =>
  names.map((name) => `${feature} | ${name}`);
const joints = Array.from({ length: 7 }, (_, i) => `joint_${i}`);
const pose = ["x", "y", "z", "rx", "ry", "rz", "gripper"];
const so101 = [
  "shoulder_pan.pos",
  "shoulder_lift.pos",
  "elbow_flex.pos",
  "wrist_flex.pos",
  "wrist_roll.pos",
  "gripper.pos",
];

describe("replayKind: FR3 datasets get the Episodes panel", () => {
  test("FR3 family with 7 joint columns: panel, joints", () => {
    const kind = replayKind("franka_panda", [
      "timestamp",
      ...keys("observation.state", joints),
    ]);
    expect(kind.kind).toBe("panel");
    if (kind.kind !== "panel") return;
    expect(kind.detection.mode).toBe("joints");
    expect(kind.family).toBe("fr3");
  });

  test("a generic arm with x,y,z,rx,ry,rz: panel, pose only, family generic", () => {
    for (const robotType of ["generic_arm", "", null, undefined]) {
      const kind = replayKind(robotType, [
        "timestamp",
        ...keys("observation.state", pose),
      ]);
      expect(kind.kind).toBe("panel");
      if (kind.kind !== "panel") return;
      expect(kind.detection.mode).toBe("pose");
      expect(kind.family).toBe("generic");
    }
  });

  test("a generic arm whose columns are joints gets nothing, not a guessed FR3", () => {
    expect(
      replayKind("generic_arm", [
        "timestamp",
        ...keys("observation.state", joints),
      ]),
    ).toEqual({ kind: "none" });
  });
});

describe("replayKind: other robots keep their own viewer", () => {
  test("SO-101 still uses the URDF viewer, whatever its columns", () => {
    const flat = ["timestamp", ...keys("observation.state", so101)];
    expect(replayKind("so101_follower", flat)).toEqual({ kind: "urdf" });
    expect(replayKind("so100", flat)).toEqual({ kind: "urdf" });
    // even if it happened to carry pose-looking columns
    expect(
      replayKind("so101_follower", [
        "timestamp",
        ...keys("observation.state", pose),
      ]),
    ).toEqual({ kind: "urdf" });
  });

  test("OpenArm and Unitree G1 still use the URDF viewer", () => {
    const flat = ["timestamp", ...keys("observation.state", ["a", "b"])];
    expect(replayKind("openarm_follower", flat)).toEqual({ kind: "urdf" });
    expect(replayKind("unitree_g1", flat)).toEqual({ kind: "urdf" });
  });

  test("franka_g1x is a Franka, not a G1: never the URDF viewer", () => {
    const withJoints = ["timestamp", ...keys("observation.state", joints)];
    const kind = replayKind("franka_g1x", withJoints);
    expect(kind.kind).toBe("panel");
    // and without usable columns it offers nothing rather than the G1 viewer
    expect(
      replayKind("franka_g1x", [
        "timestamp",
        ...keys("observation.state", ["a", "b"]),
      ]),
    ).toEqual({ kind: "none" });
  });

  test("an unknown robot has no 3D view", () => {
    expect(
      replayKind("xarm7", ["timestamp", ...keys("observation.state", joints)]),
    ).toEqual({ kind: "none" });
  });
});

describe("flatKeysOf", () => {
  test("a column missing from the first row is still found", () => {
    const rows: Record<string, number>[] = [
      { timestamp: 0 },
      { timestamp: 1, "a | x": 1 },
      { timestamp: 2, "a | x": 2 },
    ];
    expect(flatKeysOf(rows).sort()).toEqual(["a | x", "timestamp"]);
  });
  test("no rows, no keys", () => {
    expect(flatKeysOf([])).toEqual([]);
  });
});

describe("the episode viewer uses replayKind", () => {
  const source = readFileSync(
    join(
      import.meta.dir,
      "../../../app/[org]/[dataset]/[episode]/episode-viewer.tsx",
    ),
    "utf8",
  );
  test("it decides the 3D tab and the panel with replayKind, never with hasURDFSupport directly", () => {
    // hasURDFSupport -> isG1Robot treats any robot_type containing "g1" as a
    // Unitree G1 (franka_g1x too); replayKind decides the FR3 family first.
    expect(source).toContain("replayKind(");
    expect(source).not.toMatch(/\bhasURDFSupport\b/);
  });
  test("the panel loads lazily, so three.js stays out of the main viewer chunk", () => {
    expect(source).toContain(
      'lazy(() => import("@/components/robot-3d/replay-panel"))',
    );
    expect(source).not.toMatch(/^import .* from "@\/components\/robot-3d\//m);
    expect(source).not.toMatch(/^import .* from "@\/lib\/replay\/frames"/m);
  });
});
