/** Passive measured state for display; never a control/admission grant. */
export type RobotStudioStateKind =
  | "fresh"
  | "stale"
  | "missing"
  | "invalid"
  | "unavailable";

export type RobotStudioGripper =
  | { knuckle_angle_rad: number; opening_width_m?: never }
  | { opening_width_m: number; knuckle_angle_rad?: never };

export interface RobotStudioReportedEE {
  position_m: [number, number, number];
  quaternion_xyzw: [number, number, number, number];
  frame_id: string;
  end_frame: string;
}

export interface RobotStudioMeasurement {
  joints_rad: [number, number, number, number, number, number, number];
  gripper: RobotStudioGripper | null;
  reported_ee: RobotStudioReportedEE | null;
}

export type RobotStudioStateReason =
  | "sample_fresh"
  | "sample_stale"
  | "sample_in_future"
  | "file_missing"
  | "unsafe_path"
  | "not_regular"
  | "too_large"
  | "changed_during_read"
  | "invalid_json"
  | "schema_invalid"
  | "invalid_timestamp"
  | "invalid_joints"
  | "invalid_gripper"
  | "invalid_reported_ee"
  | "read_failed"
  | "clock_invalid";

export interface RobotStudioState {
  schema: "levi.robot_studio.state.v1";
  state: RobotStudioStateKind;
  reason_code: RobotStudioStateReason;
  age_s: number | null;
  sampled_at_epoch: number | null;
  sequence: number | null;
  source_id: string | null;
  /** Present only for a validated sample no older than one second. */
  measurement: RobotStudioMeasurement | null;
  capabilities: { read_only: true; motion: false };
}
