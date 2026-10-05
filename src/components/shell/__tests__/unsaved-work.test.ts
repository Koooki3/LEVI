import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import { hasUnsavedWork, setUnsavedWork } from "../unsaved-work";

describe("unsaved work", () => {
  test("a flag the frame reads", () => {
    expect(hasUnsavedWork()).toBe(false);
    setUnsavedWork(true);
    expect(hasUnsavedWork()).toBe(true);
    setUnsavedWork(false);
    expect(hasUnsavedWork()).toBe(false);
  });

  test("the annotation editor reports its draft, and clears it on leaving", () => {
    const code = readFileSync(
      join(import.meta.dir, "../../../context/annotations-context.tsx"),
      "utf8",
    );
    expect(code).toMatch(
      /useEffect\(\(\) => \{\s*setUnsavedWork\(dirty\);\s*return \(\) => setUnsavedWork\(false\);\s*\}, \[dirty\]\)/,
    );
  });
});
