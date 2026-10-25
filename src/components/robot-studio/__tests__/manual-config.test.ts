import { describe, expect, test } from "bun:test";
import {
  exportManualConfiguration,
  parseManualConfiguration,
} from "../manual-config";
import type { ManualLimits, ManualStopLimits } from "../manual-api";

// Synthetic values only: never a site configuration or motion approval.
const limits: ManualLimits = {
  box_min_m: [-1, -1, -1],
  box_max_m: [1, 1, 1],
  max_delta_m: 0.2,
  max_step_m: 0.01,
  max_velocity_m_s: 0.03,
  period_s: 0.02,
  max_reading_age_s: 0.3,
  max_anchor_age_s: 2,
  deadman_timeout_s: 0.2,
  target_timeout_s: 0.2,
  max_orientation_drift_rad: 0.02,
  quaternion_norm_tolerance: 0.001,
  max_settle_age_s: 0.3,
};
const stops: ManualStopLimits = {
  settle_window_s: 0.2,
  settle_position_m: 0.001,
  settle_angle_rad: 0.001,
  gripper_settle_rad: 0.001,
};
describe("manual numeric configuration, never motion authorization", () => {
  test("roundtrips all explicit limits without permissions or attestations", () => {
    const parsed = parseManualConfiguration(
      exportManualConfiguration(limits, stops),
    );
    expect(parsed?.limits).toEqual(limits);
    expect(parsed?.stop_limits).toEqual(stops);
    expect(Object.keys(parsed!)).toEqual(["schema", "limits", "stop_limits"]);
  });
  test("rejects malformed, missing, extra, invalid and out-of-envelope inputs", () => {
    const valid = JSON.parse(exportManualConfiguration(limits, stops));
    for (const value of [
      null,
      {},
      { ...valid, attest: { area_clear: true } },
      { ...valid, schema: "unknown" },
      { ...valid, limits: { ...limits, period_s: 0 } },
      { ...valid, limits: { ...limits, period_s: "0.02" } },
      { ...valid, limits: { ...limits, box_min_m: [-3, -1, -1] } },
      { ...valid, limits: { ...limits, box_min_m: limits.box_max_m } },
      { ...valid, stop_limits: { ...stops, settle_window_s: false } },
      { ...valid, stop_limits: {} },
      { ...valid, limits: { ...limits, extra: 1 } },
    ])
      expect(parseManualConfiguration(JSON.stringify(value))).toBeNull();
    expect(parseManualConfiguration("{")).toBeNull();
    expect(parseManualConfiguration(" ".repeat(16385))).toBeNull();
  });
});
