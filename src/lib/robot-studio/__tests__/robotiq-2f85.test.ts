import { describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { Window } from "happy-dom";
import URDFLoader from "urdf-loader";
import { Box3, Vector3, type Mesh, type Material } from "three";
import { robotGlbLoader } from "../glb-loader";
import {
  ROBOTIQ_CLOSED_ANGLE,
  ROBOTIQ_DRIVE_JOINT,
  ROBOTIQ_MODEL_URL,
  ROBOTIQ_STROKE_M,
  angleToOpeningRatio,
  angleToOpeningWidthM,
  openingRatioToAngle,
} from "../robotiq-2f85";

type URDFRobot = ReturnType<URDFLoader["parse"]>;

const base = new URL(
  "../../../../public/robots/robotiq-2f85/",
  import.meta.url,
);
const source = JSON.parse(readFileSync(new URL("SOURCE.json", base), "utf8"));
const urdf = readFileSync(new URL("robotiq-2f85.urdf", base), "utf8");

function withRobot(check: (robot: URDFRobot) => void): void {
  const window = new Window();
  const globals = globalThis as unknown as Record<string, unknown>;
  const names = ["Document", "Element", "DOMParser"] as const;
  const saved = names.map((name) => globals[name]);
  names.forEach((name) => {
    globals[name] = window[name];
  });
  try {
    const loader = new URDFLoader();
    loader.parseVisual = false;
    check(loader.parse(urdf));
  } finally {
    names.forEach((name, i) => {
      if (saved[i] === undefined) delete globals[name];
      else globals[name] = saved[i];
    });
    void window.happyDOM.abort();
  }
}

describe("Robotiq 2F-85 offline model and physical opening", () => {
  test("uses the fixed BSD source and a standalone mount without control interfaces", () => {
    expect(ROBOTIQ_MODEL_URL).toBe("/robots/robotiq-2f85/robotiq-2f85.urdf");
    expect(source.commit).toBe("a74d007d8f2f06dc6a503ad21038ba869d4999a6");
    expect(source.license).toBe("BSD-3-Clause");
    expect(source.installation_calibrated).toBe(false);
    const license = readFileSync(new URL("LICENSE", base), "utf8");
    expect(license).toContain("BSD 3-Clause License");
    expect(license).toContain("Copyright (c) 2022, PickNik Robotics");
    expect(license).toContain(
      "Redistribution and use in source and binary forms",
    );
    expect(createHash("sha256").update(license).digest("hex")).toBe(
      source.sources.LICENSE.sha256,
    );
    expect(urdf).not.toMatch(/ros2_control|hardware|plugin|xacro|\$\{/);
    expect(urdf).not.toMatch(/<collision|<inertial/);
    expect(urdf.match(/<mesh /g)).toHaveLength(9);
    expect(urdf.match(/<mimic /g)).toHaveLength(5);
    withRobot((robot) => {
      expect(Object.keys(robot.links)).toHaveLength(10);
      expect(Object.keys(robot.joints)).toHaveLength(9);
      expect(robot.joints.robotiq_85_base_joint.parent?.name).toBe(
        "gripper_mount",
      );
      expect(robot.joints.robotiq_85_base_joint.position.toArray()).toEqual([
        0, 0, 0,
      ]);
      expect(robot.joints[ROBOTIQ_DRIVE_JOINT].limit).toEqual({
        lower: 0,
        upper: 0.8,
      });
      expect(ROBOTIQ_CLOSED_ANGLE).toBe(0.7929);
      expect(ROBOTIQ_CLOSED_ANGLE).toBeLessThan(
        robot.joints[ROBOTIQ_DRIVE_JOINT].limit.upper,
      );
    });
  });

  test("open, intermediate and closed physical widths round-trip", () => {
    expect(openingRatioToAngle(1)).toBe(0);
    expect(openingRatioToAngle(0)).toBe(ROBOTIQ_CLOSED_ANGLE);
    expect(angleToOpeningWidthM(0)).toBe(ROBOTIQ_STROKE_M);
    expect(angleToOpeningWidthM(ROBOTIQ_CLOSED_ANGLE)).toBe(0);
    // Nominal geometry is close to the model's 85 mm name, not hardcoded to it.
    expect(ROBOTIQ_STROKE_M).toBeGreaterThan(0.084);
    expect(ROBOTIQ_STROKE_M).toBeLessThan(0.086);
    for (const ratio of [0, 0.001, 0.1, 0.25, 0.5, 0.75, 0.9, 0.999, 1]) {
      const angle = openingRatioToAngle(ratio);
      expect(angleToOpeningRatio(angle)).toBeCloseTo(ratio, 12);
      expect(angleToOpeningWidthM(angle)).toBeCloseTo(
        ratio * ROBOTIQ_STROKE_M,
        12,
      );
    }
    expect(
      Math.abs(openingRatioToAngle(0.5) - ROBOTIQ_CLOSED_ANGLE * 0.5),
    ).toBeGreaterThan(0.01);
  });

  test("all mimic joints follow their signed source multipliers", () => {
    const signs: Record<string, number> = {
      robotiq_85_right_knuckle_joint: -1,
      robotiq_85_left_inner_knuckle_joint: 1,
      robotiq_85_right_inner_knuckle_joint: -1,
      robotiq_85_left_finger_tip_joint: -1,
      robotiq_85_right_finger_tip_joint: 1,
    };
    withRobot((robot) => {
      for (const angle of [0, openingRatioToAngle(0.5), ROBOTIQ_CLOSED_ANGLE]) {
        robot.setJointValue(ROBOTIQ_DRIVE_JOINT, angle);
        for (const [name, sign] of Object.entries(signs)) {
          expect(robot.joints[name].jointValue[0]).toBeCloseTo(
            sign * angle,
            12,
          );
        }
      }
    });
  });

  test("width agrees with URDF tip-frame geometry and shared server derivation", () => {
    expect(source.server_derivation.conversion_excerpt).toContain(
      "THETA_CLOSED = 0.7929",
    );
    expect(source.server_derivation.conversion_excerpt).toContain(
      "_DX = 0.03152616 + 0.00563134",
    );
    expect(source.server_derivation.conversion_excerpt).toContain(
      "_DZ = -0.00376347 + 0.04718515",
    );
    withRobot((robot) => {
      const tipGap = (angle: number) => {
        robot.setJointValue(ROBOTIQ_DRIVE_JOINT, angle);
        robot.updateMatrixWorld(true);
        return (
          robot.links.robotiq_85_left_finger_tip_link.getWorldPosition(
            new Vector3(),
          ).x -
          robot.links.robotiq_85_right_finger_tip_link.getWorldPosition(
            new Vector3(),
          ).x
        );
      };
      const closedGap = tipGap(ROBOTIQ_CLOSED_ANGLE);
      for (const angle of [0, 0.2, 0.4, 0.6, ROBOTIQ_CLOSED_ANGLE]) {
        expect(angleToOpeningWidthM(angle)).toBeCloseTo(
          tipGap(angle) - closedGap,
          12,
        );
      }
    });
  });

  test("rejects non-finite, wrong-type and out-of-range values without clamping", () => {
    for (const bad of [
      NaN,
      Infinity,
      -Infinity,
      -Number.MIN_VALUE,
      1.0000001,
    ]) {
      expect(() => openingRatioToAngle(bad)).toThrow(RangeError);
    }
    for (const bad of [
      NaN,
      Infinity,
      -Infinity,
      -Number.MIN_VALUE,
      ROBOTIQ_CLOSED_ANGLE + Number.EPSILON,
      0.8,
    ]) {
      expect(() => angleToOpeningWidthM(bad)).toThrow(RangeError);
      expect(() => angleToOpeningRatio(bad)).toThrow(RangeError);
    }
    for (const bad of [null, undefined, "0.5", {}, []]) {
      expect(() => openingRatioToAngle(bad as number)).toThrow(RangeError);
      expect(() => angleToOpeningWidthM(bad as number)).toThrow(RangeError);
    }
  });

  test("all vendored files match their recorded hashes", () => {
    for (const [name, metadata] of Object.entries(source.outputs) as [
      string,
      { bytes: number; sha256: string },
    ][]) {
      const bytes = readFileSync(new URL(name, base));
      expect(bytes.byteLength).toBe(metadata.bytes);
      expect(createHash("sha256").update(bytes).digest("hex")).toBe(
        metadata.sha256,
      );
    }
  });

  test("every vendored mesh is meshopt-compressed (re-run compress-glb.py after vendoring)", () => {
    for (const name of Object.keys(source.outputs).filter((n) =>
      n.endsWith(".glb"),
    )) {
      const data = readFileSync(new URL(name, base));
      const length = data.readUInt32LE(12);
      const json = JSON.parse(data.subarray(20, 20 + length).toString("utf8"));
      expect(json.extensionsUsed ?? []).toContain("EXT_meshopt_compression");
    }
  });

  test("GLTFLoader can decode every local GLB with meter-scale geometry", async () => {
    const loader = robotGlbLoader();
    const files = Object.keys(source.outputs).filter((name) =>
      name.endsWith(".glb"),
    );
    expect(files).toHaveLength(9);
    for (const name of files) {
      const bytes = readFileSync(new URL(name, base));
      expect(bytes.readUInt32LE(0)).toBe(0x46546c67);
      expect(bytes.readUInt32LE(8)).toBe(bytes.byteLength);
      const buffer = bytes.buffer.slice(
        bytes.byteOffset,
        bytes.byteOffset + bytes.byteLength,
      ) as ArrayBuffer;
      const gltf = await loader.parseAsync(buffer, "");
      gltf.scene.updateMatrixWorld(true);
      const bounds = new Box3().setFromObject(gltf.scene);
      expect(bounds.isEmpty()).toBe(false);
      expect(
        Math.max(...bounds.getSize(new Vector3()).toArray()),
      ).toBeGreaterThan(0.005);
      expect(
        Math.max(
          ...bounds.min.toArray().map(Math.abs),
          ...bounds.max.toArray().map(Math.abs),
        ),
      ).toBeLessThan(0.2);
      gltf.scene.traverse((object) => {
        const mesh = object as Mesh;
        if (!mesh.isMesh) return;
        expect(mesh.geometry.getAttribute("position").count).toBeGreaterThan(0);
        mesh.geometry.dispose();
        for (const material of [mesh.material].flat() as Material[])
          material.dispose();
      });
    }
  });
});
