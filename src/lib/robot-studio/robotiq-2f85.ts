/** Offline Robotiq 2F-85 geometry, not hardware commands or measured feedback.
 * Derivation and hashes: public/robots/robotiq-2f85/SOURCE.json.
 * Position inputs are physical opening ratios (1=open, 0=closed), not angle fractions.
 */
export const ROBOTIQ_MODEL_URL = "/robots/robotiq-2f85/robotiq-2f85.urdf";
export const ROBOTIQ_CLOSED_ANGLE = 0.7929;
export const ROBOTIQ_DRIVE_JOINT = "robotiq_85_left_knuckle_joint";
// Original URDF link translations, mirrored by the shared Robotiq server.
const X0 = 0.03060114;
const DX = 0.03152616 + 0.00563134;
const DZ = -0.00376347 + 0.04718515;
const padX = (theta: number): number =>
  X0 + DX * Math.cos(theta) - DZ * Math.sin(theta);
const CLOSED_PAD_X = padX(ROBOTIQ_CLOSED_ANGLE);
export const ROBOTIQ_STROKE_M = 2 * (padX(0) - CLOSED_PAD_X);

function assertRange(value: number, maximum: number, label: string): void {
  if (!Number.isFinite(value) || value < 0 || value > maximum) {
    throw new RangeError(`${label} must be finite and in [0, ${maximum}]`);
  }
}

/** Nominal total opening width in meters; input is the drive-joint angle in radians. */
export function angleToOpeningWidthM(theta: number): number {
  assertRange(theta, ROBOTIQ_CLOSED_ANGLE, "Robotiq angle (rad)");
  return 2 * (padX(theta) - CLOSED_PAD_X);
}

export function angleToOpeningRatio(theta: number): number {
  return angleToOpeningWidthM(theta) / ROBOTIQ_STROKE_M;
}

/** Invert physical opening width. Uses the server's 64-step monotonic bisection. */
export function openingRatioToAngle(ratio: number): number {
  assertRange(ratio, 1, "Robotiq opening ratio");
  // Preserve exact endpoint conventions instead of returning bisection roundoff.
  if (ratio === 0) return ROBOTIQ_CLOSED_ANGLE;
  if (ratio === 1) return 0;
  const width = ratio * ROBOTIQ_STROKE_M;
  let low = 0;
  let high = ROBOTIQ_CLOSED_ANGLE;
  for (let iteration = 0; iteration < 64; iteration++) {
    const middle = 0.5 * (low + high);
    if (angleToOpeningWidthM(middle) > width) low = middle;
    else high = middle;
  }
  return 0.5 * (low + high);
}
