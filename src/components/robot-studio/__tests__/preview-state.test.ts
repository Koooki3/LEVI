import { describe, expect, test } from "bun:test";
import { Quaternion } from "three";
import {
  FR3_INITIAL_JOINTS,
  forwardPose,
} from "@/lib/robot-studio/fr3-kinematics";
import {
  exportPreview,
  parsePoseFields,
  poseFields,
  previewWaypoint,
  type PoseFields,
} from "../preview-state";

describe("offline preview input and export", () => {
  test("round-trips meters and xyzw through millimeters and XYZ degrees", () => {
    const pose = forwardPose(FR3_INITIAL_JOINTS);
    const parsed = parsePoseFields(poseFields(pose))!;
    pose.position.forEach((value, i) =>
      expect(Math.abs(parsed.position[i] - value)).toBeLessThan(0.00006),
    );
    expect(
      new Quaternion(...pose.quaternion).angleTo(
        new Quaternion(...parsed.quaternion),
      ),
    ).toBeLessThan(0.002);
  });
  test("rejects blanks, nonfinite values and out of range target fields", () => {
    for (const value of [
      "",
      " ",
      "NaN",
      "Infinity",
      "-Infinity",
      "2001",
      "-2001",
    ]) {
      expect(parsePoseFields([value, "0", "400", "0", "0", "0"])).toBeNull();
    }
    const fields: PoseFields = ["300", "0", "400", "361", "0", "0"];
    expect(parsePoseFields(fields)).toBeNull();
    expect(
      parsePoseFields(["300", "0", "400", "0", "0", "180"])?.position,
    ).toEqual([0.3, 0, 0.4]);
  });
  test("exports only a non-executable draft with an explicit flange frame", () => {
    const source = [...FR3_INITIAL_JOINTS];
    const saved = previewWaypoint(source);
    source[0] = 0.2;
    expect(saved.joints[0]).toBe(FR3_INITIAL_JOINTS[0]);
    // Recompute stored poses from validated joints, never trust a stale UI target.
    saved.pose.position[0] = 999;
    const output = JSON.parse(exportPreview([saved]));
    expect(output.hardware_execution).toBe(false);
    expect(output.collision_checked).toBe(false);
    expect(output.calibrated_tool).toBe(false);
    expect(output.end_frame).toBe("fr3_link8");
    expect(output.units.quaternion).toBe("xyzw");
    expect(output.waypoints[0].pose).toEqual(forwardPose(FR3_INITIAL_JOINTS));
  });
  test("rejects empty/oversized drafts and nonfinite joint data", () => {
    expect(() => exportPreview([])).toThrow();
    const point = previewWaypoint(FR3_INITIAL_JOINTS);
    expect(() => exportPreview(Array(101).fill(point))).toThrow();
    point.joints[1] = NaN;
    expect(() => exportPreview([point])).toThrow();
  });
});
