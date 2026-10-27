/** Offline, nominal FR3 flange kinematics. Never a motion or collision-safety API.
 * Chain and limits: public/robots/fr3/SOURCE.json (same source as the rendered URDF).
 * Positions are meters in fr3_link0; quaternion order is [x,y,z,w].
 */
import { Euler, Matrix4, Quaternion, Vector3 } from "three";
import chain from "./fr3-chain.json";

export type Position3 = [number, number, number];
export type Quaternion4 = [number, number, number, number];
export interface RobotPose {
  position: Position3;
  quaternion: Quaternion4;
}
export interface PoseSolution {
  converged: boolean;
  /** On failure this is exactly the supplied seed, never a partial solution. */
  joints: number[];
  positionError: number;
  orientationError: number;
  iterations: number;
  reason?: "invalid-input" | "unreachable" | "no-convergence";
}
export const FR3_FLANGE_FRAME = "fr3_link8";
export const FR3_JOINT_NAMES = Object.freeze(
  chain.slice(0, 7).map((j) => j.name),
);
export const FR3_JOINT_LIMITS = Object.freeze(
  chain
    .slice(0, 7)
    .map((j) =>
      Object.freeze({ lower: j.limit!.lower, upper: j.limit!.upper }),
    ),
);
/** An illustrative, in-limit preview pose, not measured robot state or a home command. */
export const FR3_INITIAL_JOINTS: readonly number[] = Object.freeze([
  0,
  -Math.PI / 4,
  0,
  (-3 * Math.PI) / 4,
  0,
  Math.PI / 2,
  Math.PI / 4,
]);
export const FR3_POSITION_TOLERANCE = 0.0005;
export const FR3_ORIENTATION_TOLERANCE = 0.002;
const ORIENTATION_WEIGHT = 0.35;
const MAX_ITERATIONS = 120;
const MAX_STEP = 0.12;
const origins = chain.map((j) =>
  new Matrix4().compose(
    new Vector3(j.xyz[0], j.xyz[1], j.xyz[2]),
    new Quaternion().setFromEuler(
      new Euler(j.rpy[0], j.rpy[1], j.rpy[2], "ZYX"),
    ),
    new Vector3(1, 1, 1),
  ),
);
const finiteArray = (values: readonly number[], length: number) =>
  Array.isArray(values) &&
  values.length === length &&
  Array.from(values).every(Number.isFinite);
export const jointsWithinLimits = (joints: readonly number[]): boolean =>
  finiteArray(joints, 7) &&
  joints.every(
    (q, i) => q >= FR3_JOINT_LIMITS[i].lower && q <= FR3_JOINT_LIMITS[i].upper,
  );

function evaluate(joints: readonly number[]) {
  const transform = new Matrix4();
  const positions: Vector3[] = [];
  const axes: Vector3[] = [];
  for (let i = 0; i < 8; i++) {
    transform.multiply(origins[i]);
    if (i < 7) {
      positions.push(new Vector3().setFromMatrixPosition(transform));
      axes.push(new Vector3(0, 0, 1).transformDirection(transform));
      transform.multiply(new Matrix4().makeRotationZ(joints[i]));
    }
  }
  return {
    position: new Vector3().setFromMatrixPosition(transform),
    quaternion: new Quaternion().setFromRotationMatrix(transform).normalize(),
    positions,
    axes,
  };
}

export function forwardPose(joints: readonly number[]): RobotPose {
  if (!jointsWithinLimits(joints))
    throw new RangeError(
      "Expected seven finite FR3 joints within official limits",
    );
  const pose = evaluate(joints);
  return {
    position: pose.position.toArray() as Position3,
    quaternion: pose.quaternion.toArray() as Quaternion4,
  };
}

function rotationError(target: Quaternion, current: Quaternion): Vector3 {
  const delta = target.clone().multiply(current.clone().invert()).normalize();
  // q and -q represent the same orientation. Choose the shortest rotation.
  if (delta.w < 0) delta.set(-delta.x, -delta.y, -delta.z, -delta.w);
  const sinHalf = Math.hypot(delta.x, delta.y, delta.z);
  if (sinHalf < 1e-12)
    return new Vector3(2 * delta.x, 2 * delta.y, 2 * delta.z);
  return new Vector3(delta.x, delta.y, delta.z).multiplyScalar(
    (2 * Math.atan2(sinHalf, delta.w)) / sinHalf,
  );
}

/** Small positive-definite 6x6 linear solve (damping prevents singular pivots). */
function solveLinear(matrix: number[][], rhs: number[]): number[] | null {
  const rows = matrix.map((row, i) => [...row, rhs[i]]);
  for (let col = 0; col < 6; col++) {
    let pivot = col;
    for (let i = col + 1; i < 6; i++)
      if (Math.abs(rows[i][col]) > Math.abs(rows[pivot][col])) pivot = i;
    if (
      !Number.isFinite(rows[pivot][col]) ||
      Math.abs(rows[pivot][col]) < 1e-14
    )
      return null;
    [rows[col], rows[pivot]] = [rows[pivot], rows[col]];
    const scale = rows[col][col];
    for (let j = col; j <= 6; j++) rows[col][j] /= scale;
    for (let i = 0; i < 6; i++)
      if (i !== col) {
        const factor = rows[i][col];
        for (let j = col; j <= 6; j++) rows[i][j] -= factor * rows[col][j];
      }
  }
  const result = rows.map((row) => row[6]);
  return result.every(Number.isFinite) ? result : null;
}

/** Seeded, bounded damped least squares. No random restart or branch jump.
 * A reachable target may fail near limits or a singularity; callers must keep
 * their last accepted pose unless converged is true. This does not certify
 * self-collision, environment collision, dynamics, calibration or hardware safety.
 */
export function solvePose(
  target: RobotPose,
  seed: readonly number[],
): PoseSolution {
  const fail = (
    reason: PoseSolution["reason"],
    p = Infinity,
    r = Infinity,
    iterations = 0,
  ): PoseSolution => ({
    converged: false,
    joints: Array.isArray(seed) ? [...seed] : [],
    positionError: p,
    orientationError: r,
    iterations,
    reason,
  });
  if (
    !jointsWithinLimits(seed) ||
    !target ||
    !finiteArray(target.position, 3) ||
    !finiteArray(target.quaternion, 4)
  )
    return fail("invalid-input");
  const quaternionNorm = Math.hypot(...target.quaternion);
  if (!Number.isFinite(quaternionNorm) || quaternionNorm < 1e-10)
    return fail("invalid-input");
  const desiredPosition = new Vector3(...target.position);
  const desiredQuaternion = new Quaternion(
    ...(target.quaternion.map((v) => v / quaternionNorm) as Quaternion4),
  );
  // Sum of all fixed translations is a conservative geometric reach bound.
  const reachBound = chain.reduce((sum, j) => sum + Math.hypot(...j.xyz), 0);
  if (Math.hypot(...target.position) > reachBound) return fail("unreachable");
  let joints = [...seed];
  const errors = (pose: ReturnType<typeof evaluate>) => {
    const p = desiredPosition.clone().sub(pose.position);
    const r = rotationError(desiredQuaternion, pose.quaternion);
    return {
      p,
      r,
      score: p.lengthSq() + ORIENTATION_WEIGHT ** 2 * r.lengthSq(),
    };
  };
  let pose = evaluate(joints);
  let error = errors(pose);
  for (let iteration = 0; iteration <= MAX_ITERATIONS; iteration++) {
    if (
      error.p.length() <= FR3_POSITION_TOLERANCE &&
      error.r.length() <= FR3_ORIENTATION_TOLERANCE
    ) {
      return {
        converged: true,
        joints,
        positionError: error.p.length(),
        orientationError: error.r.length(),
        iterations: iteration,
      };
    }
    if (iteration === MAX_ITERATIONS) break;
    const columns = pose.axes.map((axis, i) => {
      const translation = axis
        .clone()
        .cross(pose.position.clone().sub(pose.positions[i]));
      return [
        ...translation.toArray(),
        ...axis.clone().multiplyScalar(ORIENTATION_WEIGHT).toArray(),
      ];
    });
    const rhs = [
      ...error.p.toArray(),
      ...error.r.clone().multiplyScalar(ORIENTATION_WEIGHT).toArray(),
    ];
    let accepted = false;
    for (const damping of [0.015, 0.05, 0.15]) {
      const matrix = Array.from({ length: 6 }, (_, row) =>
        Array.from(
          { length: 6 },
          (_, col) =>
            columns.reduce(
              (sum, values) => sum + values[row] * values[col],
              0,
            ) + (row === col ? damping * damping : 0),
        ),
      );
      const projected = solveLinear(matrix, rhs);
      if (!projected) continue;
      const step = columns.map((column) =>
        column.reduce((sum, v, i) => sum + v * projected[i], 0),
      );
      const scale = Math.min(
        1,
        MAX_STEP / Math.max(...step.map(Math.abs), 1e-12),
      );
      for (const fraction of [1, 0.5, 0.25, 0.125]) {
        const candidate = joints.map((q, i) =>
          Math.max(
            FR3_JOINT_LIMITS[i].lower,
            Math.min(FR3_JOINT_LIMITS[i].upper, q + step[i] * scale * fraction),
          ),
        );
        const candidatePose = evaluate(candidate);
        const candidateError = errors(candidatePose);
        if (candidateError.score < error.score - 1e-14) {
          joints = candidate;
          pose = candidatePose;
          error = candidateError;
          accepted = true;
          break;
        }
      }
      if (accepted) break;
    }
    if (!accepted)
      return fail(
        "no-convergence",
        error.p.length(),
        error.r.length(),
        iteration + 1,
      );
  }
  return fail(
    "no-convergence",
    error.p.length(),
    error.r.length(),
    MAX_ITERATIONS,
  );
}
