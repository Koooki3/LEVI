import { Euler, Quaternion } from "three";
import { forwardPose } from "@/lib/robot-studio/fr3-kinematics";
import {
  angleToOpeningWidthM,
  openingRatioToAngle,
} from "@/lib/robot-studio/robotiq-2f85";
import {
  cloneToolDraft,
  cloneTeleopDraft,
  type ToolDraft,
  type TeleopDraft,
} from "./tool-draft";
import {
  cloneWorkspaceBox,
  pointInWorkspace,
  type WorkspaceBox,
} from "./workspace-draft";

export type PreviewPose = {
  position: [number, number, number];
  quaternion: [number, number, number, number];
};
export type PoseFields = [string, string, string, string, string, string];
export type ToolPreview = { openingRatio: number; draft: ToolDraft };
export type Waypoint = {
  joints: number[];
  pose: PreviewPose;
  tool?: {
    opening_ratio: number;
    opening_width_m: number;
    mount_draft: PreviewPose | null;
    tcp_draft: PreviewPose | null;
  };
};
export type ExecutionDraft = {
  teleopDraft: TeleopDraft;
  workspaceBox: WorkspaceBox | null;
};
export const MAX_WAYPOINTS = 100;
const RAD = Math.PI / 180;

export function poseFields(pose: PreviewPose): PoseFields {
  const euler = new Euler().setFromQuaternion(
    new Quaternion(...pose.quaternion),
    "XYZ",
  );
  return [
    ...pose.position.map((v) => (v * 1000).toFixed(1)),
    ...[euler.x, euler.y, euler.z].map((v) => (v / RAD).toFixed(1)),
  ] as PoseFields;
}

export function parsePoseFields(fields: PoseFields): PreviewPose | null {
  if (fields.some((v) => !v.trim() || !Number.isFinite(Number(v)))) return null;
  const v = fields.map(Number);
  if (
    v.slice(0, 3).some((x) => Math.abs(x) > 2000) ||
    v.slice(3).some((x) => Math.abs(x) > 360)
  )
    return null;
  const q = new Quaternion().setFromEuler(
    new Euler(v[3] * RAD, v[4] * RAD, v[5] * RAD, "XYZ"),
  );
  return {
    position: [v[0] / 1000, v[1] / 1000, v[2] / 1000],
    quaternion: [q.x, q.y, q.z, q.w],
  };
}

export function previewWaypoint(
  joints: readonly number[],
  tool?: ToolPreview,
): Waypoint {
  const pose = forwardPose([...joints]);
  const saved: Waypoint = {
    joints: [...joints],
    pose: { position: [...pose.position], quaternion: [...pose.quaternion] },
  };
  if (tool) {
    const angle = openingRatioToAngle(tool.openingRatio);
    const draft = cloneToolDraft(tool.draft);
    saved.tool = {
      opening_ratio: tool.openingRatio,
      opening_width_m: angleToOpeningWidthM(angle),
      mount_draft: draft.mount,
      tcp_draft: draft.tcp,
    };
  }
  return saved;
}

/** A design draft, never a robot command or a recorded demonstration. */
export function exportPreview(
  waypoints: readonly Waypoint[],
  config?: ExecutionDraft,
): string {
  if (!waypoints.length || waypoints.length > MAX_WAYPOINTS)
    throw new Error("Invalid waypoint count");
  const workspaceBox = config ? cloneWorkspaceBox(config.workspaceBox) : null;
  const teleop = config ? cloneTeleopDraft(config.teleopDraft) : undefined;
  return JSON.stringify(
    {
      format:
        config || waypoints.some((p) => p.tool)
          ? "levi_robot_preview_v2"
          : "levi_robot_preview_v1",
      robot: "fr3",
      base_frame: "fr3_link0",
      end_frame: "fr3_link8",
      units: { position: "m", joints: "rad", quaternion: "xyzw" },
      hardware_execution: false,
      collision_checked: false,
      calibrated_tool: false,
      ...(config
        ? {
            execution_draft: {
              motion_enabled: false,
              teleop,
              workspace_box: workspaceBox
                ? {
                    frame_id: "fr3_link0",
                    target_frame: "fr3_link8",
                    min_m: workspaceBox.min,
                    max_m: workspaceBox.max,
                    checks_tool_sweep: false,
                  }
                : null,
            },
          }
        : {}),
      waypoints: waypoints.map((point) => {
        const saved = previewWaypoint(
          point.joints,
          point.tool
            ? {
                openingRatio: point.tool.opening_ratio,
                draft: {
                  mount: point.tool.mount_draft,
                  tcp: point.tool.tcp_draft,
                },
              }
            : undefined,
        );
        return config
          ? {
              ...saved,
              within_workspace_draft: pointInWorkspace(
                workspaceBox,
                saved.pose.position,
              ),
            }
          : saved;
      }),
    },
    null,
    2,
  );
}
