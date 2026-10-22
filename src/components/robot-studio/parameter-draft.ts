/** Reviewed software capabilities, never a live device capability/parameter readback. */
export const PARAMETER_CAPABILITIES = [
  { id: "preview", mode: "preview_only" },
  { id: "impedance", mode: "acknowledgement_pending" },
  { id: "jointTorque", mode: "unsupported" },
  { id: "acceleration", mode: "unsupported" },
  { id: "externalForce", mode: "unsupported" },
  { id: "gripper", mode: "restart_required" },
] as const;

/** Proposed limits only, explicitly distinct from torque/force control targets. */
export const PARAMETER_FIELDS = [
  { key: "joint_torque_limit_nm", label: "jointTorque", count: 7, unit: "N·m" },
  {
    key: "joint_acceleration_limit_rad_s2",
    label: "jointAcceleration",
    count: 7,
    unit: "rad/s²",
  },
  {
    key: "cartesian_acceleration_limit_m_s2",
    label: "cartesianAcceleration",
    count: 1,
    unit: "m/s²",
  },
  {
    key: "external_force_stop_threshold_n",
    label: "externalForce",
    count: 1,
    unit: "N",
  },
] as const;
export type ParameterKey = (typeof PARAMETER_FIELDS)[number]["key"];
export type ParameterFields = Record<ParameterKey, string[]>;
export type ParameterDraft = Record<ParameterKey, number[] | null>;
export type ParsedParameters = {
  draft: ParameterDraft;
  invalid: ParameterKey[];
};

export const emptyParameterFields = (): ParameterFields =>
  Object.fromEntries(
    PARAMETER_FIELDS.map(({ key, count }) => [
      key,
      Array<string>(count).fill(""),
    ]),
  ) as ParameterFields;

/** Blanks mean unknown. Partial vectors and nonpositive/nonfinite values cannot be exported. */
export function parseParameterFields(
  fields: ParameterFields,
): ParsedParameters {
  const draft = {} as ParameterDraft;
  const invalid: ParameterKey[] = [];
  for (const { key, count } of PARAMETER_FIELDS) {
    const values = fields[key];
    draft[key] = null;
    if (
      !Array.isArray(values) ||
      values.length !== count ||
      !Array.from(values).every((v) => typeof v === "string")
    ) {
      invalid.push(key);
      continue;
    }
    if (values.every((v) => v.trim() === "")) continue;
    const parsed = values.map(Number);
    if (
      values.some((v) => v.trim() === "") ||
      parsed.some((v) => !Number.isFinite(v) || v <= 0)
    ) {
      invalid.push(key);
      continue;
    }
    draft[key] = parsed;
  }
  return { draft, invalid };
}

export function exportParameterDraft(fields: ParameterFields): string {
  const { draft, invalid } = parseParameterFields(fields);
  if (invalid.length)
    throw new RangeError("Incomplete or invalid parameter draft");
  return JSON.stringify(
    {
      schema: "levi.robot_studio.parameter_proposal.v1",
      source: "browser_draft",
      hardware_execution: false,
      hardware_values_read: false,
      approved_limits: false,
      proposed_limits: draft,
      capabilities: PARAMETER_CAPABILITIES.map(({ id, mode }) => ({
        id,
        mode,
        can_apply: false,
      })),
    },
    null,
    2,
  );
}
