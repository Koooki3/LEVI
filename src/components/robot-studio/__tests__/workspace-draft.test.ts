import { describe, expect, test } from "bun:test";
import {
  cloneWorkspaceBox,
  isValidWorkspaceBox,
  parseWorkspaceFields,
  pointInWorkspace,
  workspaceFields,
  type WorkspaceBox,
  type WorkspaceFields,
} from "../workspace-draft";

const box = (): WorkspaceBox => ({
  min: [-0.5, -0.4, 0.1],
  max: [0.5, 0.4, 0.9],
});
const fields = (): WorkspaceFields => [
  "-500",
  "-400",
  "100",
  "500",
  "400",
  "900",
];

describe("offline workspace boundary draft", () => {
  test("unset stays null and is never unrestricted approval", () => {
    expect(parseWorkspaceFields(["", " ", "", "", "", ""])).toBeNull();
    expect(workspaceFields(null)).toEqual(["", "", "", "", "", ""]);
    expect(cloneWorkspaceBox(null)).toBeNull();
    expect(isValidWorkspaceBox(null)).toBe(false);
    expect(pointInWorkspace(null, [0, 0, 0])).toBeNull();
    expect(pointInWorkspace(null, [999, -999, 999])).toBeNull();
    expect(() => pointInWorkspace(null, [NaN, 0, 0])).toThrow(RangeError);
  });

  test("millimeter fields round-trip to meters without rounding away narrow bounds", () => {
    expect(parseWorkspaceFields(fields())).toEqual(box());
    expect(workspaceFields(box())).toEqual(fields());
    const narrow: WorkspaceBox = {
      min: [0.000001, -0.123456789, 0.234567891],
      max: [0.000002, -0.123456788, 0.234567892],
    };
    const result = parseWorkspaceFields(workspaceFields(narrow))!;
    for (const bound of ["min", "max"] as const)
      narrow[bound].forEach((value, i) =>
        expect(result[bound][i]).toBeCloseTo(value, 14),
      );
    expect(isValidWorkspaceBox(result)).toBe(true);
    expect(
      parseWorkspaceFields([
        "-2e3",
        "-2000",
        "-2000",
        "+2000",
        "2000.0",
        "2e3",
      ]),
    ).toEqual({ min: [-2, -2, -2], max: [2, 2, 2] });
  });

  test("inclusive faces and corners, with exterior points rejected", () => {
    const value = box();
    for (const point of [
      [0, 0, 0.5],
      value.min,
      value.max,
      [-0.5, 0.4, 0.9],
      [0.5, -0.4, 0.1],
    ] as [number, number, number][]) {
      expect(pointInWorkspace(value, point)).toBe(true);
    }
    for (const point of [
      [-0.500001, 0, 0.5],
      [0.500001, 0, 0.5],
      [0, -0.400001, 0.5],
      [0, 0.400001, 0.5],
      [0, 0, 0.099999],
      [0, 0, 0.900001],
    ] as [number, number, number][]) {
      expect(pointInWorkspace(value, point)).toBe(false);
    }
  });

  test("clone owns both coordinate arrays", () => {
    const original = box();
    const cloned = cloneWorkspaceBox(original)!;
    expect(cloned).toEqual(original);
    expect(cloned).not.toBe(original);
    expect(cloned.min).not.toBe(original.min);
    expect(cloned.max).not.toBe(original.max);
    cloned.min[0] = -0.8;
    cloned.max[0] = 0.8;
    expect(original).toEqual(box());
  });

  test("partial empty, reversed, flat and out-of-envelope boxes throw", () => {
    for (let axis = 0; axis < 6; axis++) {
      const partial = fields();
      partial[axis] = "";
      expect(() => parseWorkspaceFields(partial)).toThrow(RangeError);
    }
    for (let axis = 0; axis < 3; axis++) {
      for (const max of ["-500", "-501"]) {
        const invalid = [
          "-500",
          "-500",
          "-500",
          "500",
          "500",
          "500",
        ] as WorkspaceFields;
        invalid[axis + 3] = max;
        expect(() => parseWorkspaceFields(invalid)).toThrow(RangeError);
      }
    }
    for (const bad of [
      "-2000.001",
      "2000.001",
      "NaN",
      "Infinity",
      "-Infinity",
      "1e999",
      "0x10",
      "abc",
    ]) {
      const invalid = fields();
      invalid[0] = bad;
      expect(() => parseWorkspaceFields(invalid)).toThrow(RangeError);
    }
  });

  test("invalid box shapes and non-finite points never become an inside result", () => {
    for (const invalid of [
      undefined,
      {},
      { min: [0, 0], max: [1, 1, 1] },
      { min: [0, 0, 0], max: [0, 1, 1] },
      { min: [NaN, 0, 0], max: [1, 1, 1] },
      { min: [-3, 0, 0], max: [1, 1, 1] },
      { min: [0, 0, 0], max: [1, Infinity, 1] },
      { min: new Array(3), max: [1, 1, 1] },
    ]) {
      expect(isValidWorkspaceBox(invalid)).toBe(false);
      expect(() => cloneWorkspaceBox(invalid as WorkspaceBox)).toThrow(
        RangeError,
      );
      expect(() =>
        pointInWorkspace(invalid as WorkspaceBox, [0, 0, 0]),
      ).toThrow(RangeError);
      expect(() => workspaceFields(invalid as WorkspaceBox)).toThrow(
        RangeError,
      );
    }
    for (const bad of [NaN, Infinity, -Infinity]) {
      expect(() => pointInWorkspace(box(), [bad, 0, 0])).toThrow(RangeError);
    }
    expect(() =>
      pointInWorkspace(box(), new Array(3) as [number, number, number]),
    ).toThrow(RangeError);
    expect(() => parseWorkspaceFields(new Array(6) as WorkspaceFields)).toThrow(
      RangeError,
    );
  });
});
