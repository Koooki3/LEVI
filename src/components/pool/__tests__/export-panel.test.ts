import { describe, expect, test } from "bun:test";
import { outputDirProblem } from "../export-panel";
import { bytes, duration } from "../pool-progress";

describe("outputDirProblem", () => {
  const roots = ["/data/exports/"];
  test("accepts empty, inside and home paths", () => {
    expect(outputDirProblem("", roots)).toBeNull();
    expect(outputDirProblem("/data/exports", roots)).toBeNull();
    expect(outputDirProblem("/data/exports/pi05/", roots)).toBeNull();
    expect(outputDirProblem("~/x", roots)).toBeNull();
  });
  test("refuses outside and relative folders", () => {
    expect(outputDirProblem("/data/exports2", roots)).toBe("outside");
    expect(outputDirProblem("/etc", roots)).toBe("outside");
    expect(outputDirProblem("relative/dir", roots)).toContain("absolute");
  });
});

describe("progress formatting", () => {
  test("bytes and duration", () => {
    expect(bytes(0)).toBe("0 B");
    expect(bytes(1536)).toBe("1.5 KB");
    expect(duration(65)).toBe("1m 05s");
    expect(duration(null)).toBe("—");
  });
});
