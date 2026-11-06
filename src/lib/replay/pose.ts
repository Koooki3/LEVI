// Pose columns -> orientation, for the 3D replay's coordinate axes.
//
// The converted datasets store orientation as rx, ry, rz from
// `levi/conversion/raw.py::euler`: roll = atan2(2(wx+yz), 1-2(x²+y²)),
// pitch = asin(2(wy-zx)), yaw = atan2(2(wz+xy), 1-2(y²+z²)), unwrapped. That
// is the aerospace roll-pitch-yaw: rotate about the FIXED X by roll, then the
// fixed Y by pitch, then the fixed Z by yaw, i.e. R = Rz(yaw)·Ry(pitch)·Rx(roll)
// (extrinsic XYZ).
//
// three.js `Euler(x, y, z, "XYZ")` is the opposite order, R = Rx·Ry·Rz
// (intrinsic XYZ), so `new Euler(rx, ry, rz)` draws every rotation that uses
// two axes wrong. The matching three order for these angles is "ZYX" (the
// URDF `rpy` convention, which `fr3-kinematics.ts` already uses). This module
// computes the quaternion directly so that no order has to be chosen at the
// call site; `__tests__/pose.test.ts` pins it with known rotations.
//
// Quaternions are [x, y, z, w], the order of `observation.state | qx..qw` and
// of three.js `Quaternion.set`.

export type Quat = [number, number, number, number];

/** Quaternion of roll/pitch/yaw radians, extrinsic XYZ (R = Rz·Ry·Rx). */
export function quaternionFromRpy(
  roll: number,
  pitch: number,
  yaw: number,
  out: Quat | number[] = [0, 0, 0, 1],
): Quat {
  const cr = Math.cos(roll / 2);
  const sr = Math.sin(roll / 2);
  const cp = Math.cos(pitch / 2);
  const sp = Math.sin(pitch / 2);
  const cy = Math.cos(yaw / 2);
  const sy = Math.sin(yaw / 2);
  out[0] = sr * cp * cy - cr * sp * sy;
  out[1] = cr * sp * cy + sr * cp * sy;
  out[2] = cr * cp * sy - sr * sp * cy;
  out[3] = cr * cp * cy + sr * sp * sy;
  return out as Quat;
}

/** Unit-length copy of a stored quaternion; an all-zero one becomes identity. */
export function normalizedQuaternion(
  x: number,
  y: number,
  z: number,
  w: number,
  out: Quat | number[] = [0, 0, 0, 1],
): Quat {
  const norm = Math.hypot(x, y, z, w);
  if (!(norm > 1e-12)) {
    out[0] = out[1] = out[2] = 0;
    out[3] = 1;
    return out as Quat;
  }
  out[0] = x / norm;
  out[1] = y / norm;
  out[2] = z / norm;
  out[3] = w / norm;
  return out as Quat;
}

/** Rotate the vector v by the unit quaternion q (used by tests and by checks). */
export function rotateVector(
  q: readonly number[],
  v: readonly [number, number, number],
): [number, number, number] {
  const [qx, qy, qz, qw] = q;
  const [vx, vy, vz] = v;
  // t = 2 q.xyz × v;  v' = v + w t + q.xyz × t
  const tx = 2 * (qy * vz - qz * vy);
  const ty = 2 * (qz * vx - qx * vz);
  const tz = 2 * (qx * vy - qy * vx);
  return [
    vx + qw * tx + (qy * tz - qz * ty),
    vy + qw * ty + (qz * tx - qx * tz),
    vz + qw * tz + (qx * ty - qy * tx),
  ];
}
