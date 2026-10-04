import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";

describe("globals.css", () => {
  test("Tailwind does not scan the documentation", () => {
    const css = readFileSync(join(import.meta.dir, "../globals.css"), "utf8");
    // Tailwind v4: a source path relative to this file is excluded.
    expect(css).toMatch(/@source not "\.\.\/\.\.\/docs";/);
  });
});
