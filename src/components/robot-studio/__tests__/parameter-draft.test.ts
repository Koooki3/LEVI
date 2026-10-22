import { describe, expect, test } from "bun:test";
import {
  emptyParameterFields,
  exportParameterDraft,
  PARAMETER_FIELDS,
  parseParameterFields,
} from "../parameter-draft";

describe("parameter proposals have no hardware effect or inferred limits", () => {
  test("unknown values stay null and exports cannot claim execution, readback or approval", () => {
    const fields = emptyParameterFields();
    const parsed = parseParameterFields(fields);
    expect(parsed.invalid).toEqual([]);
    expect(Object.values(parsed.draft).every((value) => value === null)).toBe(
      true,
    );
    const result = JSON.parse(exportParameterDraft(fields));
    expect(result.schema).toBe("levi.robot_studio.parameter_proposal.v1");
    expect(result.source).toBe("browser_draft");
    expect(result.hardware_execution).toBe(false);
    expect(result.hardware_values_read).toBe(false);
    expect(result.approved_limits).toBe(false);
    expect(
      result.capabilities.every(
        (entry: { can_apply: boolean }) => entry.can_apply === false,
      ),
    ).toBe(true);
  });

  test("each joint group must be complete; partial input cannot silently become unknown", () => {
    for (const { key, count } of PARAMETER_FIELDS.filter(
      (field) => field.count === 7,
    )) {
      const fields = emptyParameterFields();
      fields[key][0] = "1";
      expect(parseParameterFields(fields).invalid).toContain(key);
      expect(() => exportParameterDraft(fields)).toThrow();
      fields[key] = Array.from({ length: count }, (_, i) => String(i + 1));
      expect(parseParameterFields(fields).draft[key]).toEqual([
        1, 2, 3, 4, 5, 6, 7,
      ]);
    }
  });

  test("nonfinite, nonpositive, malformed and sparse groups cannot be exported", () => {
    for (const { key, count } of PARAMETER_FIELDS) {
      for (const bad of ["0", "-1", "NaN", "Infinity", "1e309"]) {
        const fields = emptyParameterFields();
        fields[key] = Array<string>(count).fill("1");
        fields[key][0] = bad;
        expect(parseParameterFields(fields).invalid).toContain(key);
        expect(() => exportParameterDraft(fields)).toThrow();
      }
      for (const bad of [[], new Array(count), undefined, [1]]) {
        const fields = { ...emptyParameterFields(), [key]: bad };
        expect(parseParameterFields(fields as never).invalid).toContain(key);
      }
    }
  });

  test("preserves units and proposed numbers without inventing upper limits or changing the input", () => {
    const fields = emptyParameterFields();
    fields.joint_torque_limit_nm = ["1", "2", "3", "4", "5", "6", "7"];
    fields.cartesian_acceleration_limit_m_s2 = ["0.0123"];
    fields.external_force_stop_threshold_n = ["1000000000"];
    const before = JSON.stringify(fields);
    const out = JSON.parse(exportParameterDraft(fields));
    expect(out.proposed_limits.joint_torque_limit_nm).toEqual([
      1, 2, 3, 4, 5, 6, 7,
    ]);
    expect(out.proposed_limits.joint_acceleration_limit_rad_s2).toBeNull();
    expect(out.proposed_limits.cartesian_acceleration_limit_m_s2).toEqual([
      0.0123,
    ]);
    expect(out.proposed_limits.external_force_stop_threshold_n).toEqual([
      1000000000,
    ]);
    expect(out.approved_limits).toBe(false);
    expect(JSON.stringify(fields)).toBe(before);
    fields.joint_torque_limit_nm[0] = "90";
    expect(out.proposed_limits.joint_torque_limit_nm[0]).toBe(1);
    expect(emptyParameterFields().joint_torque_limit_nm[0]).toBe("");
  });
});
