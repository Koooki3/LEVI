import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";

const config = readFileSync(
  join(import.meta.dir, "../../../../eslint.config.mjs"),
  "utf8",
);

describe("the colour rule of the linter", () => {
  test("covers every interface source, not a list of migrated files", () => {
    expect(config).toContain('files: ["src/**/*.{ts,tsx}"]');
    expect(config).toContain("ignores: HEX_EXCEPTIONS");
    expect(config).not.toMatch(/FRAME_FILES|PAGE_FILES|VIEWER_FILES/);
  });

  test("every exception says why", () => {
    const list = /const HEX_EXCEPTIONS = \[([\s\S]*?)\n\];/.exec(config)?.[1];
    expect(list).toBeTruthy();
    const lines = list!.split("\n").filter((line) => line.trim());
    lines.forEach((line, index) => {
      if (!line.trim().startsWith('"')) return;
      // A comment line sits directly above each entry.
      expect(lines[index - 1]?.trim().startsWith("//")).toBe(true);
    });
  });

  test("it also stops colour functions with literal numbers", () => {
    expect(config).toContain("COLOR_FUNCTION");
  });
});
