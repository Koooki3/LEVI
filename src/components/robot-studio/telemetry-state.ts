import { jointsWithinLimits } from "@/lib/robot-studio/fr3-kinematics";
import {
  ROBOTIQ_CLOSED_ANGLE,
  ROBOTIQ_STROKE_M,
  angleToOpeningRatio,
} from "@/lib/robot-studio/robotiq-2f85";
import type {
  RobotStudioMeasurement,
  RobotStudioState,
} from "@/types/robotStudio";

export const TELEMETRY_MAX_AGE_MS = 1000;

function vector(value: unknown, size: number): value is number[] {
  return (
    Array.isArray(value) &&
    value.length === size &&
    Array.from(value).every((x) => typeof x === "number" && Number.isFinite(x))
  );
}

export function validTelemetryState(value: unknown): value is RobotStudioState {
  if (!value || typeof value !== "object") return false;
  const state = value as RobotStudioState;
  if (
    state.schema !== "levi.robot_studio.state.v1" ||
    state.capabilities?.read_only !== true ||
    state.capabilities?.motion !== false ||
    !["fresh", "stale", "missing", "invalid", "unavailable"].includes(
      state.state,
    )
  )
    return false;
  if (state.state !== "fresh") return state.measurement === null;
  if (
    typeof state.age_s !== "number" ||
    !Number.isFinite(state.age_s) ||
    state.age_s < 0 ||
    !Number.isInteger(state.sequence) ||
    (state.sequence ?? -1) < 0 ||
    typeof state.sampled_at_epoch !== "number" ||
    !Number.isFinite(state.sampled_at_epoch) ||
    typeof state.source_id !== "string" ||
    !state.source_id
  )
    return false;
  const measurement = state.measurement;
  if (
    !measurement ||
    !vector(measurement.joints_rad, 7) ||
    !jointsWithinLimits(measurement.joints_rad)
  )
    return false;
  const gripper = measurement.gripper;
  if (gripper !== null) {
    if (!gripper || typeof gripper !== "object") return false;
    const angle = "knuckle_angle_rad" in gripper;
    const key = angle ? gripper.knuckle_angle_rad : gripper.opening_width_m;
    if (typeof key !== "number" || !Number.isFinite(key) || key < 0)
      return false;
    if (angle && "opening_width_m" in gripper) return false;
  }
  const ee = measurement.reported_ee;
  if (
    ee !== null &&
    (!ee ||
      !vector(ee.position_m, 3) ||
      !vector(ee.quaternion_xyzw, 4) ||
      Math.abs(Math.hypot(...ee.quaternion_xyzw) - 1) > 0.001 ||
      typeof ee.frame_id !== "string" ||
      typeof ee.end_frame !== "string")
  )
    return false;
  return true;
}

/** Wall-clock timestamps are validated by the reader; UI expiry uses elapsed monotonic time. */
export function freshMeasurement(
  state: RobotStudioState | null,
  elapsedMs: number,
): RobotStudioMeasurement | null {
  if (
    !state ||
    state.state !== "fresh" ||
    !validTelemetryState(state) ||
    !Number.isFinite(elapsedMs) ||
    elapsedMs < 0 ||
    state.age_s! * 1000 + elapsedMs > TELEMETRY_MAX_AGE_MS
  )
    return null;
  return state.measurement;
}

/** A source value outside the model's stroke stays unavailable; never invent a display opening. */
export function measuredOpeningRatio(
  measurement: RobotStudioMeasurement | null,
): number | null {
  const gripper = measurement?.gripper;
  if (!gripper) return null;
  if ("knuckle_angle_rad" in gripper) {
    const angle = gripper.knuckle_angle_rad!;
    return angle >= 0 && angle <= ROBOTIQ_CLOSED_ANGLE
      ? angleToOpeningRatio(angle)
      : null;
  }
  const width = gripper.opening_width_m!;
  return width >= 0 && width <= ROBOTIQ_STROKE_M
    ? width / ROBOTIQ_STROKE_M
    : null;
}
