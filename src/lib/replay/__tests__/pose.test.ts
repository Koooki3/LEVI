import { describe, expect, test } from "bun:test";
import { Euler, Quaternion } from "three";
import {
  normalizedQuaternion,
  quaternionFromRpy,
  rotateVector,
  type Quat,
} from "../pose";

const close = (a: readonly number[], b: readonly number[], digits = 9) =>
  a.forEach((value, i) => expect(value).toBeCloseTo(b[i], digits));
/** A quaternion and its negative are the same rotation. */
const sameRotation = (a: Quat, b: Quat) => {
  const sign =
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3] < 0 ? -1 : 1;
  close(
    a,
    b.map((v) => v * sign),
  );
};
/** `levi/conversion/raw.py::euler` for one quaternion (x, y, z, w), without the unwrap. */
function rawPyEuler([x, y, z, w]: Quat): [number, number, number] {
  return [
    Math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)),
    Math.asin(Math.max(-1, Math.min(1, 2 * (w * y - z * x)))),
    Math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)),
  ];
}

describe("quaternionFromRpy (raw.py roll/pitch/yaw, extrinsic XYZ)", () => {
  test("no rotation is the identity", () => {
    close(quaternionFromRpy(0, 0, 0), [0, 0, 0, 1]);
  });

  test("one axis at a time turns the vector the right-handed way", () => {
    // roll about X: Y -> Z; pitch about Y: Z -> X; yaw about Z: X -> Y.
    close(
      rotateVector(quaternionFromRpy(Math.PI / 2, 0, 0), [0, 1, 0]),
      [0, 0, 1],
    );
    close(
      rotateVector(quaternionFromRpy(0, Math.PI / 2, 0), [0, 0, 1]),
      [1, 0, 0],
    );
    close(
      rotateVector(quaternionFromRpy(0, 0, Math.PI / 2), [1, 0, 0]),
      [0, 1, 0],
    );
  });

  test("known two-axis rotation: roll 90 deg, then yaw 90 deg about the FIXED Z", () => {
    const q = quaternionFromRpy(Math.PI / 2, 0, Math.PI / 2);
    // X is unchanged by the roll, then the fixed-Z yaw sends it to +Y.
    close(rotateVector(q, [1, 0, 0]), [0, 1, 0]);
    // Y goes to +Z by the roll; the yaw leaves Z alone.
    close(rotateVector(q, [0, 1, 0]), [0, 0, 1]);
  });

  test("it equals three's order ZYX, and NOT three's Euler XYZ (the pitfall)", () => {
    const [roll, pitch, yaw] = [0.4, -0.7, 1.1];
    const ours = quaternionFromRpy(roll, pitch, yaw);
    const zyx = new Quaternion().setFromEuler(
      new Euler(roll, pitch, yaw, "ZYX"),
    );
    sameRotation(ours, [zyx.x, zyx.y, zyx.z, zyx.w]);
    // Roll 90 deg then yaw 90 deg: the X axis ends on +Y here, on +Z with
    // three's intrinsic "XYZ".
    const half = Math.PI / 2;
    const xyz = new Quaternion().setFromEuler(new Euler(half, 0, half, "XYZ"));
    close(rotateVector([xyz.x, xyz.y, xyz.z, xyz.w], [1, 0, 0]), [0, 0, 1]);
    close(rotateVector(quaternionFromRpy(half, 0, half), [1, 0, 0]), [0, 1, 0]);
  });

  test("angles produced by raw.py's formulas from a quaternion give that quaternion back", () => {
    for (const q of [
      normalizedQuaternion(0.1, 0.2, 0.3, 0.9),
      normalizedQuaternion(-0.5, 0.1, 0.7, 0.4),
      normalizedQuaternion(0.9, -0.1, 0.05, 0.4),
    ]) {
      const [roll, pitch, yaw] = rawPyEuler(q);
      sameRotation(quaternionFromRpy(roll, pitch, yaw), q);
    }
  });

  test("an unwrapped angle past pi is the same rotation as its wrapped value", () => {
    const wrapped = quaternionFromRpy(3.0, 0.2, -1.0);
    const unwrapped = quaternionFromRpy(
      3.0 + 2 * Math.PI,
      0.2,
      -1.0 - 2 * Math.PI,
    );
    sameRotation(wrapped, unwrapped);
  });

  test("writes into the buffer it is given", () => {
    const out = [9, 9, 9, 9];
    expect(quaternionFromRpy(0, 0, Math.PI, out)).toBe(out as unknown as Quat);
    close(out, [0, 0, 1, 0]);
  });
});

describe("normalizedQuaternion", () => {
  test("scales to unit length", () => {
    close(normalizedQuaternion(0, 0, 0, 2), [0, 0, 0, 1]);
    const q = normalizedQuaternion(1, 2, 3, 4);
    expect(Math.hypot(...q)).toBeCloseTo(1, 12);
  });
  test("a zero or non-finite quaternion becomes the identity, not NaN", () => {
    close(normalizedQuaternion(0, 0, 0, 0), [0, 0, 0, 1]);
    close(normalizedQuaternion(NaN, 0, 0, 1), [0, 0, 0, 1]);
  });
});
