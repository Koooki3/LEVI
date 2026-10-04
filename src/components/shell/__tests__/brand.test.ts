import { describe, expect, test } from "bun:test";
import { existsSync, readFileSync } from "fs";
import { join } from "path";
import { LEVI_MARK, LEVI_MARK_PATH } from "../brand";

const app = join(import.meta.dir, "../../../app");

describe("LEVI mark", () => {
  test("the tab icon is drawn from the same shape as the page mark", () => {
    const svg = readFileSync(join(app, "icon.svg"), "utf8");
    expect(svg).toContain(`viewBox="0 0 ${LEVI_MARK.size} ${LEVI_MARK.size}"`);
    expect(svg).toContain(`rx="${LEVI_MARK.radius}"`);
    expect(svg).toContain(`d="${LEVI_MARK_PATH}"`);
    // Readable on dark browser chrome too.
    expect(svg).toContain("prefers-color-scheme:dark");
  });

  test("touch icon and favicon.ico exist next to it", () => {
    expect(existsSync(join(app, "apple-icon.png"))).toBe(true);
    expect(existsSync(join(app, "favicon.ico"))).toBe(true);
  });

  test("the glyph stays inside the tile with a margin", () => {
    for (const [x, y, w, h] of LEVI_MARK.rects) {
      expect(x).toBeGreaterThanOrEqual(6);
      expect(y).toBeGreaterThanOrEqual(6);
      expect(x + w).toBeLessThanOrEqual(LEVI_MARK.size - 6);
      expect(y + h).toBeLessThanOrEqual(LEVI_MARK.size - 6);
    }
  });
});
