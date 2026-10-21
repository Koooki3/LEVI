import { Euler, Quaternion } from "three";
import { forwardPose } from "@/lib/robot-studio/fr3-kinematics";

export type PreviewPose = {
  position: [number, number, number];
  quaternion: [number, number, number, number];
};
export type PoseFields = [string, string, string, string, string, string];
export type Waypoint = { joints: number[]; pose: PreviewPose };
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

export function previewWaypoint(joints: readonly number[]): Waypoint {
  const pose = forwardPose([...joints]);
  return {
    joints: [...joints],
    pose: { position: [...pose.position], quaternion: [...pose.quaternion] },
  };
}

/** A design draft, never a robot command or a recorded demonstration. */
export function exportPreview(waypoints: readonly Waypoint[]): string {
  if (!waypoints.length || waypoints.length > MAX_WAYPOINTS)
    throw new Error("Invalid waypoint count");
  return JSON.stringify(
    {
      format: "levi_robot_preview_v1",
      robot: "fr3",
      base_frame: "fr3_link0",
      end_frame: "fr3_link8",
      units: { position: "m", joints: "rad", quaternion: "xyzw" },
      hardware_execution: false,
      collision_checked: false,
      calibrated_tool: false,
      waypoints: waypoints.map((point) => previewWaypoint(point.joints)),
    },
    null,
    2,
  );
}
