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

import { DEFAULT_TOOL_DRAFT, EMPTY_TELEOP_DRAFT } from "../tool-draft";
import { ROBOTIQ_STROKE_M } from "@/lib/robot-studio/robotiq-2f85";

describe("tool-aware non-executable export", () => {
  test("keeps a per-waypoint tool snapshot and physical width instead of a binary gripper", () => {
    const draft = {
      mount: {
        position: [0, 0, 0.017] as [number, number, number],
        quaternion: [0, 0, 0, 1] as [number, number, number, number],
      },
      tcp: null,
    };
    const saved = previewWaypoint(FR3_INITIAL_JOINTS, {
      openingRatio: 0.35,
      draft,
    });
    draft.mount.position[2] = 1;
    const output = JSON.parse(
      exportPreview([saved], {
        teleopDraft: EMPTY_TELEOP_DRAFT,
        workspaceBox: null,
      }),
    );
    expect(output.format).toBe("levi_robot_preview_v2");
    expect(output.hardware_execution).toBe(false);
    expect(output.calibrated_tool).toBe(false);
    expect(output.execution_draft.motion_enabled).toBe(false);
    expect(output.execution_draft.workspace_box).toBeNull();
    expect(output.waypoints[0].tool.opening_ratio).toBe(0.35);
    expect(output.waypoints[0].tool.opening_width_m).toBeCloseTo(
      0.35 * ROBOTIQ_STROKE_M,
      12,
    );
    expect(output.waypoints[0].tool.mount_draft.position[2]).toBe(0.017);
    expect(output.waypoints[0].tool.tcp_draft).toBeNull();
    expect(output.waypoints[0].within_workspace_draft).toBeNull();
  });
  test("new boundary drafts identify old outside points without relabeling them executable", () => {
    const point = previewWaypoint(FR3_INITIAL_JOINTS);
    const output = JSON.parse(
      exportPreview([point], {
        teleopDraft: EMPTY_TELEOP_DRAFT,
        workspaceBox: { min: [0, 0, 0], max: [0.1, 0.1, 0.1] },
      }),
    );
    expect(output.waypoints[0].within_workspace_draft).toBe(false);
    expect(output.execution_draft.workspace_box.frame_id).toBe("fr3_link0");
    expect(output.execution_draft.workspace_box.target_frame).toBe("fr3_link8");
    expect(output.execution_draft.workspace_box.checks_tool_sweep).toBe(false);
    expect(output.hardware_execution).toBe(false);
  });
  test("invalid gripper, transform, speed and boundary inputs cannot be exported", () => {
    for (const ratio of [NaN, Infinity, -0.01, 1.01])
      expect(() =>
        previewWaypoint(FR3_INITIAL_JOINTS, {
          openingRatio: ratio,
          draft: DEFAULT_TOOL_DRAFT,
        }),
      ).toThrow();
    const point = previewWaypoint(FR3_INITIAL_JOINTS);
    expect(() =>
      exportPreview([point], {
        teleopDraft: { ...EMPTY_TELEOP_DRAFT, linearSpeedMps: Infinity },
        workspaceBox: null,
      }),
    ).toThrow();
    expect(() =>
      exportPreview([point], {
        teleopDraft: EMPTY_TELEOP_DRAFT,
        workspaceBox: { min: [0, 0, 0], max: [0, 1, 1] },
      }),
    ).toThrow();
  });
});
