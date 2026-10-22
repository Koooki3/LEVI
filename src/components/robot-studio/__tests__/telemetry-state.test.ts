import { describe, expect, test } from "bun:test";
import {
  FR3_INITIAL_JOINTS,
  FR3_JOINT_LIMITS,
  forwardPose,
} from "@/lib/robot-studio/fr3-kinematics";
import {
  ROBOTIQ_CLOSED_ANGLE,
  ROBOTIQ_STROKE_M,
} from "@/lib/robot-studio/robotiq-2f85";
import type { RobotStudioState } from "@/types/robotStudio";
import {
  freshMeasurement,
  measuredOpeningRatio,
  validTelemetryState,
} from "../telemetry-state";

const sample = (): RobotStudioState => ({
  schema: "levi.robot_studio.state.v1",
  state: "fresh",
  reason_code: "sample_fresh",
  age_s: 0.2,
  sampled_at_epoch: 1800000000,
  sequence: 4,
  source_id: "fixture",
  measurement: {
    joints_rad: [...FR3_INITIAL_JOINTS],
    gripper: { knuckle_angle_rad: 0 },
    reported_ee: null,
  },
  capabilities: { read_only: true, motion: false },
});

describe("passive measured-state display", () => {
  test("expires a fresh response by monotonic elapsed time even without another response", () => {
    const state = sample();
    expect(validTelemetryState(state)).toBe(true);
    expect(freshMeasurement(state, 700)).toBe(state.measurement);
    expect(freshMeasurement(state, 800.01)).toBeNull();
    expect(freshMeasurement(state, Infinity)).toBeNull();
    expect(freshMeasurement(state, -1)).toBeNull();
    expect(
      freshMeasurement({ ...state, state: "stale", measurement: null }, 0),
    ).toBeNull();
  });
  test("does not apply corrupt or actuator-capable response envelopes", () => {
    for (const state of [
      null,
      {},
      { ...sample(), capabilities: { read_only: false, motion: true } },
      { ...sample(), age_s: NaN },
      { ...sample(), sequence: true },
      {
        ...sample(),
        measurement: { ...sample().measurement!, joints_rad: new Array(7) },
      },
      {
        ...sample(),
        measurement: {
          ...sample().measurement!,
          joints_rad: [99, 0, 0, 0, 0, 0, 0],
        },
      },
      {
        ...sample(),
        measurement: {
          ...sample().measurement!,
          reported_ee: {
            position_m: [0, 0, 0],
            quaternion_xyzw: [0, 0, 0, 0],
            frame_id: "base",
            end_frame: "tool",
          },
        },
      },
    ])
      expect(validTelemetryState(state)).toBe(false);
  });
  test("uses the same strict joint limits as model FK without clamping feedback", () => {
    for (let index = 0; index < 7; index++) {
      const limit = FR3_JOINT_LIMITS[index];
      for (const bound of [limit.lower, limit.upper]) {
        const state = sample();
        state.measurement!.joints_rad[index] = bound;
        expect(validTelemetryState(state)).toBe(true);
        expect(() => forwardPose(state.measurement!.joints_rad)).not.toThrow();
      }
      for (const value of [limit.lower - 5e-7, limit.upper + 5e-7]) {
        const state = sample();
        state.measurement!.joints_rad[index] = value;
        expect(validTelemetryState(state)).toBe(false);
        expect(freshMeasurement(state, 0)).toBeNull();
        expect(state.measurement!.joints_rad[index]).toBe(value);
      }
    }
  });
  test("model opening comes only from available measured gripper state", () => {
    const m = sample().measurement!;
    expect(measuredOpeningRatio(m)).toBe(1);
    expect(
      measuredOpeningRatio({
        ...m,
        gripper: { knuckle_angle_rad: ROBOTIQ_CLOSED_ANGLE },
      }),
    ).toBe(0);
    expect(
      measuredOpeningRatio({
        ...m,
        gripper: { opening_width_m: ROBOTIQ_STROKE_M / 2 },
      }),
    ).toBe(0.5);
    expect(
      measuredOpeningRatio({
        ...m,
        gripper: { opening_width_m: ROBOTIQ_STROKE_M + 0.01 },
      }),
    ).toBeNull();
    expect(
      measuredOpeningRatio({ ...m, gripper: { knuckle_angle_rad: -0.01 } }),
    ).toBeNull();
    expect(measuredOpeningRatio({ ...m, gripper: null })).toBeNull();
    expect(measuredOpeningRatio(null)).toBeNull();
  });
});
