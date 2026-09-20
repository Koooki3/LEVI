import { afterEach, describe, expect, test } from "bun:test";
import { nextMark, setObjectMarks } from "@/components/object-marks";

afterEach(() => setObjectMarks([]));

describe("object marks", () => {
  test("sorts and de-duplicates the annotated instants", () => {
    setObjectMarks([12, 4, 4, 8]);
    expect(nextMark(0, 1)).toBe(4);
    expect(nextMark(4, 1)).toBe(8);
    expect(nextMark(12, 1)).toBeNull();
  });

  test("steps backwards to the previous annotated instant", () => {
    setObjectMarks([4, 8, 12]);
    expect(nextMark(12, -1)).toBe(8);
    expect(nextMark(4, -1)).toBeNull();
  });

  test("does not stick on the instant the viewer is already at", () => {
    setObjectMarks([4, 8]);
    expect(nextMark(4.01, 1)).toBe(8);
    expect(nextMark(7.99, -1)).toBe(4);
  });

  test("reports nothing when a dataset has no object annotations", () => {
    setObjectMarks([]);
    expect(nextMark(5, 1)).toBeNull();
    expect(nextMark(5, -1)).toBeNull();
  });
});
