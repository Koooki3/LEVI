import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { Window } from "happy-dom";
import URDFLoader from "urdf-loader";
import { Box3, Quaternion, Vector3 } from "three";
import { robotGlbLoader } from "../glb-loader";
import {
  FR3_FLANGE_FRAME,
  FR3_INITIAL_JOINTS,
  FR3_JOINT_LIMITS,
  FR3_JOINT_NAMES,
  forwardPose,
  jointsWithinLimits,
  solvePose,
} from "../fr3-kinematics";

const base = new URL("../../../../public/robots/fr3/", import.meta.url);
const initial = [...FR3_INITIAL_JOINTS];
const near = (a: readonly number[], b: readonly number[], tolerance = 1e-9) =>
  a.forEach((value, i) =>
    expect(Math.abs(value - b[i])).toBeLessThan(tolerance),
  );

describe("nominal offline FR3 flange", () => {
  test("FK matches independently loaded URDF joint transforms at several poses", () => {
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
      const robot = loader.parse(
        readFileSync(new URL("fr3.urdf", base), "utf8"),
      );
      for (const joints of [
        initial,
        [0.4, -0.5, 0.3, -1.9, -0.4, 2.0, 0.2],
        [-1, 0.6, -0.7, -2.5, 0.8, 1.3, -0.8],
      ]) {
        joints.forEach((q, i) => robot.setJointValue(FR3_JOINT_NAMES[i], q));
        robot.updateMatrixWorld(true);
        const position = robot.links[FR3_FLANGE_FRAME].getWorldPosition(
          new Vector3(),
        );
        const quaternion = robot.links[FR3_FLANGE_FRAME].getWorldQuaternion(
          new Quaternion(),
        );
        const actual = forwardPose(joints);
        near(actual.position, position.toArray());
        expect(
          Math.abs(new Quaternion(...actual.quaternion).dot(quaternion)),
        ).toBeCloseTo(1, 10);
      }
    } finally {
      names.forEach((name, i) => {
        if (saved[i] === undefined) delete globals[name];
        else globals[name] = saved[i];
      });
    }
  });

  test("IK reaches FK-generated targets from the current seed within limits", () => {
    for (const joints of [
      initial,
      [0.2, -0.7, 0.15, -2.1, 0.1, 1.8, 0.6],
      [-0.3, -0.5, -0.25, -2.0, -0.2, 1.5, 1.0],
      [0.6, 0.2, -0.4, -1.7, 0.5, 2.2, -0.4],
    ]) {
      const solution = solvePose(forwardPose(joints), initial);
      expect(solution.converged).toBe(true);
      expect(jointsWithinLimits(solution.joints)).toBe(true);
      expect(solution.positionError).toBeLessThanOrEqual(0.0005);
      expect(solution.orientationError).toBeLessThanOrEqual(0.002);
    }
  });

  test("unchanged target stays at the exact seed, and quaternion sign is equivalent", () => {
    const target = forwardPose(initial);
    target.quaternion = target.quaternion.map(
      (x) => -x,
    ) as typeof target.quaternion;
    const result = solvePose(target, initial);
    expect(result.converged).toBe(true);
    expect(result.joints).toEqual(initial);
    expect(result.iterations).toBe(0);
  });

  test("small repeated dragging stays continuous", () => {
    let current = initial;
    for (let step = 1; step <= 50; step++) {
      const q = initial.map(
        (value, i) => value + (i % 2 ? -1 : 1) * step * 0.002,
      );
      const result = solvePose(forwardPose(q), current);
      expect(result.converged).toBe(true);
      expect(
        Math.max(
          ...result.joints.map((value, i) => Math.abs(value - current[i])),
        ),
      ).toBeLessThan(0.04);
      current = result.joints;
    }
  });

  test("unreachable and non-convergent targets never expose a partial solution", () => {
    const target = forwardPose(initial);
    const result = solvePose({ ...target, position: [10, 0, 0] }, initial);
    expect(result.converged).toBe(false);
    expect(result.reason).toBe("unreachable");
    expect(result.joints).toEqual(initial);
    const impossible = solvePose({ ...target, position: [0, 0, 0] }, initial);
    expect(impossible.converged).toBe(false);
    expect(impossible.joints).toEqual(initial);
    expect(initial).toEqual(FR3_INITIAL_JOINTS);
  });

  test("rejects invalid finite values, malformed quaternion and out-of-limit seeds", () => {
    const pose = forwardPose(initial);
    for (const value of [NaN, Infinity, -Infinity]) {
      expect(
        solvePose({ ...pose, position: [value, 0, 0] }, initial).reason,
      ).toBe("invalid-input");
      expect(
        solvePose({ ...pose, quaternion: [0, value, 0, 1] }, initial).reason,
      ).toBe("invalid-input");
      expect(() => forwardPose([value, ...initial.slice(1)])).toThrow();
    }
    expect(
      solvePose({ ...pose, quaternion: [0, 0, 0, 0] }, initial).converged,
    ).toBe(false);
    for (let i = 0; i < 7; i++) {
      for (const q of [
        FR3_JOINT_LIMITS[i].lower - 0.001,
        FR3_JOINT_LIMITS[i].upper + 0.001,
      ]) {
        const invalid = initial.map((v, j) => (i === j ? q : v));
        expect(jointsWithinLimits(invalid)).toBe(false);
        expect(solvePose(pose, invalid).reason).toBe("invalid-input");
      }
    }
    expect(solvePose(pose, initial.slice(1)).reason).toBe("invalid-input");
    expect(solvePose(pose, new Array(7)).reason).toBe("invalid-input");
  });

  test("GLTFLoader decodes every local visual with meter-scale link coordinates", async () => {
    const loader = robotGlbLoader();
    for (let link = 0; link < 8; link++) {
      const data = readFileSync(new URL(`link${link}.glb`, base));
      const buffer = data.buffer.slice(
        data.byteOffset,
        data.byteOffset + data.byteLength,
      ) as ArrayBuffer;
      const gltf = await loader.parseAsync(buffer, "");
      gltf.scene.updateMatrixWorld(true);
      const bounds = new Box3().setFromObject(gltf.scene);
      expect(bounds.isEmpty()).toBe(false);
      const size = bounds.getSize(new Vector3());
      expect(Math.max(...size.toArray())).toBeGreaterThan(0.05);
      expect(
        Math.max(
          ...bounds.min.toArray().map(Math.abs),
          ...bounds.max.toArray().map(Math.abs),
        ),
      ).toBeLessThan(0.5);
      if (link === 1) {
        // Upstream shoulder DAE is in mm with a -0.192 m offset (row-major
        // matrix). After meshopt compression the node also carries the
        // dequantization, so check where the geometry lands, not the node:
        // the bounds of the uncompressed conversion, to 0.01 mm.
        const expected = [-0.055, -0.1294, -0.192, 0.055, 0.055, 0.055];
        const got = [...bounds.min.toArray(), ...bounds.max.toArray()];
        got.forEach((v, i) =>
          expect(Math.abs(v - expected[i])).toBeLessThan(1e-5),
        );
      }
      gltf.scene.traverse((object) => {
        if ("geometry" in object)
          (object.geometry as { dispose(): void }).dispose();
      });
    }
  });

  test("every vendored mesh is meshopt-compressed (re-run compress-glb.py after vendoring)", () => {
    for (let link = 0; link < 8; link++) {
      const data = readFileSync(new URL(`link${link}.glb`, base));
      const length = data.readUInt32LE(12);
      const json = JSON.parse(data.subarray(20, 20 + length).toString("utf8"));
      expect(json.extensionsUsed ?? []).toContain("EXT_meshopt_compression");
    }
  });

  test("vendored outputs match recorded hashes and valid GLB chunk lengths", () => {
    const source = JSON.parse(
      readFileSync(new URL("SOURCE.json", base), "utf8"),
    );
    for (const [name, metadata] of Object.entries(source.outputs) as [
      string,
      { bytes: number; sha256: string },
    ][]) {
      const bytes = readFileSync(new URL(name, base));
      expect(bytes.byteLength).toBe(metadata.bytes);
      expect(createHash("sha256").update(bytes).digest("hex")).toBe(
        metadata.sha256,
      );
      if (name.endsWith(".glb")) {
        expect(bytes.readUInt32LE(0)).toBe(0x46546c67);
        expect(bytes.readUInt32LE(8)).toBe(bytes.length);
        const jsonLength = bytes.readUInt32LE(12);
        const gltf = JSON.parse(
          bytes.subarray(20, 20 + jsonLength).toString("utf8"),
        );
        expect(gltf.meshes.length).toBeGreaterThan(0);
        expect(gltf.materials.length).toBeGreaterThan(0);
        expect(bytes.readUInt32LE(20 + jsonLength)).toBe(
          gltf.buffers[0].byteLength,
        );
      }
    }
  });
});
