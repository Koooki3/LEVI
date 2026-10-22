import { Euler, Quaternion } from "three";
import type { PoseFields, PreviewPose } from "./preview-state";

/** Unmeasured transforms are null, never an implicit identity calibration. */
export type ToolDraft = {
  mount: PreviewPose | null;
  /** TCP is relative to the gripper base, not a replacement flange IK target. */
  tcp: PreviewPose | null;
};

export const DEFAULT_TOOL_DRAFT: ToolDraft = Object.freeze({
  mount: null,
  tcp: null,
});

/** Configuration draft only; no site-specific operating defaults are inferred. */
export type TeleopDraft = {
  linearSpeedMps: number | null;
  angularSpeedRadps: number | null;
  positionStepMm: number | null;
  rotationStepDeg: number | null;
  commandTimeoutMs: number | null;
  gripperRatePerSec: number | null;
};

export const TELEOP_DRAFT_KEYS = [
  "linearSpeedMps",
  "angularSpeedRadps",
  "positionStepMm",
  "rotationStepDeg",
  "commandTimeoutMs",
  "gripperRatePerSec",
] as const satisfies readonly (keyof TeleopDraft)[];

export const EMPTY_TELEOP_DRAFT: TeleopDraft = Object.freeze({
  linearSpeedMps: null,
  angularSpeedRadps: null,
  positionStepMm: null,
  rotationStepDeg: null,
  commandTimeoutMs: null,
  gripperRatePerSec: null,
});

export type TeleopDraftFields = Record<keyof TeleopDraft, string>;

const RAD = Math.PI / 180;
const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const finiteTuple = (value: unknown, length: number): value is number[] =>
  Array.isArray(value) &&
  value.length === length &&
  Array.from(value).every(
    (item) => typeof item === "number" && Number.isFinite(item),
  );

/** Validate rather than silently normalizing a corrupt quaternion. */
export function isValidPreviewPose(value: unknown): value is PreviewPose {
  return (
    record(value) &&
    finiteTuple(value.position, 3) &&
    finiteTuple(value.quaternion, 4) &&
    Math.abs(Math.hypot(...value.quaternion) - 1) <= 0.000001
  );
}

export function isValidToolDraft(value: unknown): value is ToolDraft {
  return (
    record(value) &&
    (value.mount === null || isValidPreviewPose(value.mount)) &&
    (value.tcp === null || isValidPreviewPose(value.tcp))
  );
}

function clonePose(pose: PreviewPose | null): PreviewPose | null {
  return pose === null
    ? null
    : {
        position: [...pose.position],
        quaternion: [...pose.quaternion],
      };
}

export function cloneToolDraft(value: ToolDraft): ToolDraft {
  if (!isValidToolDraft(value)) throw new RangeError("Invalid tool draft");
  return { mount: clonePose(value.mount), tcp: clonePose(value.tcp) };
}

/** Independent of preview-state at runtime: its exporter consumes these helpers. */
export function toolPoseFields(pose: PreviewPose | null): PoseFields {
  if (pose === null) return ["", "", "", "", "", ""];
  if (!isValidPreviewPose(pose)) throw new RangeError("Invalid tool pose");
  const euler = new Euler().setFromQuaternion(
    new Quaternion(...pose.quaternion),
    "XYZ",
  );
  return [
    ...pose.position.map((value) => (value * 1000).toFixed(1)),
    ...[euler.x, euler.y, euler.z].map((value) => (value / RAD).toFixed(1)),
  ] as PoseFields;
}

/** Millimeters/XYZ degrees match the flange editor's broad UI field ranges. */
export function parseToolPoseFields(fields: PoseFields): PreviewPose | null {
  if (
    !Array.isArray(fields) ||
    fields.length !== 6 ||
    !Array.from(fields).every(
      (value) =>
        typeof value === "string" &&
        value.trim() !== "" &&
        Number.isFinite(Number(value)),
    )
  )
    return null;
  const values = fields.map(Number);
  if (
    values.slice(0, 3).some((value) => Math.abs(value) > 2000) ||
    values.slice(3).some((value) => Math.abs(value) > 360)
  )
    return null;
  const quaternion = new Quaternion().setFromEuler(
    new Euler(values[3] * RAD, values[4] * RAD, values[5] * RAD, "XYZ"),
  );
  const pose: PreviewPose = {
    position: [values[0] / 1000, values[1] / 1000, values[2] / 1000],
    quaternion: [quaternion.x, quaternion.y, quaternion.z, quaternion.w],
  };
  return isValidPreviewPose(pose) ? pose : null;
}

export function isValidTeleopDraft(value: unknown): value is TeleopDraft {
  return (
    record(value) &&
    TELEOP_DRAFT_KEYS.every(
      (key) =>
        value[key] === null ||
        (typeof value[key] === "number" &&
          Number.isFinite(value[key]) &&
          value[key] > 0),
    )
  );
}

export function cloneTeleopDraft(value: TeleopDraft): TeleopDraft {
  if (!isValidTeleopDraft(value))
    throw new RangeError("Invalid teleoperation draft");
  return Object.fromEntries(
    TELEOP_DRAFT_KEYS.map((key) => [key, value[key]]),
  ) as TeleopDraft;
}

export function teleopDraftFields(value: TeleopDraft): TeleopDraftFields {
  if (!isValidTeleopDraft(value))
    throw new RangeError("Invalid teleoperation draft");
  return Object.fromEntries(
    TELEOP_DRAFT_KEYS.map((key) => [
      key,
      value[key] === null ? "" : String(value[key]),
    ]),
  ) as TeleopDraftFields;
}

/** Blank means unspecified; positive finite values are drafts, not approved limits. */
export function parseTeleopDraftFields(
  fields: TeleopDraftFields,
): TeleopDraft | null {
  if (!record(fields)) return null;
  const draft = { ...EMPTY_TELEOP_DRAFT };
  for (const key of TELEOP_DRAFT_KEYS) {
    const field = fields[key];
    if (typeof field !== "string") return null;
    const text = field.trim();
    if (text === "") continue;
    const value = Number(text);
    if (!Number.isFinite(value) || value <= 0) return null;
    draft[key] = value;
  }
  return draft;
}
